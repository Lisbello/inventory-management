from datetime import datetime, timedelta
from itertools import count
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Optional
from pydantic import BaseModel, Field
from mock_data import inventory_items, orders, demand_forecasts, backlog_items, spending_summary, monthly_spending, category_spending, recent_transactions, purchase_orders

app = FastAPI(title="Factory Inventory Management System")

# Quarter mapping for date filtering
QUARTER_MAP = {
    'Q1-2025': ['2025-01', '2025-02', '2025-03'],
    'Q2-2025': ['2025-04', '2025-05', '2025-06'],
    'Q3-2025': ['2025-07', '2025-08', '2025-09'],
    'Q4-2025': ['2025-10', '2025-11', '2025-12']
}

def filter_by_month(items: list, month: Optional[str]) -> list:
    """Filter items by month/quarter based on order_date field"""
    if not month or month == 'all':
        return items

    if month.startswith('Q'):
        # Handle quarters
        if month in QUARTER_MAP:
            months = QUARTER_MAP[month]
            return [item for item in items if any(m in item.get('order_date', '') for m in months)]
    else:
        # Direct month match
        return [item for item in items if month in item.get('order_date', '')]

    return items

def apply_filters(items: list, warehouse: Optional[str] = None, category: Optional[str] = None,
                 status: Optional[str] = None) -> list:
    """Apply common filters to a list of items"""
    filtered = items

    if warehouse and warehouse != 'all':
        filtered = [item for item in filtered if item.get('warehouse') == warehouse]

    if category and category != 'all':
        filtered = [item for item in filtered if item.get('category', '').lower() == category.lower()]

    if status and status != 'all':
        filtered = [item for item in filtered if item.get('status', '').lower() == status.lower()]

    return filtered

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Data models
class InventoryItem(BaseModel):
    id: str
    sku: str
    name: str
    category: str
    warehouse: str
    quantity_on_hand: int
    reorder_point: int
    unit_cost: float
    location: str
    last_updated: str

class Order(BaseModel):
    id: str
    order_number: str
    customer: str
    items: List[dict]
    status: str
    order_date: str
    expected_delivery: str
    total_value: float
    actual_delivery: Optional[str] = None
    warehouse: Optional[str] = None
    category: Optional[str] = None

class DemandForecast(BaseModel):
    id: str
    item_sku: str
    item_name: str
    current_demand: int
    forecasted_demand: int
    trend: str
    period: str
    unit_cost: float
    lead_time_days: int

class BacklogItem(BaseModel):
    id: str
    order_id: str
    item_sku: str
    item_name: str
    quantity_needed: int
    quantity_available: int
    days_delayed: int
    priority: str
    has_purchase_order: Optional[bool] = False

class PurchaseOrder(BaseModel):
    id: str
    backlog_item_id: str
    supplier_name: str
    quantity: int
    unit_cost: float
    expected_delivery_date: str
    status: str
    created_date: str
    notes: Optional[str] = None

class CreatePurchaseOrderRequest(BaseModel):
    backlog_item_id: str
    supplier_name: str = Field(min_length=1)
    quantity: int = Field(gt=0)
    unit_cost: float = Field(gt=0)
    expected_delivery_date: str
    notes: Optional[str] = None

class Task(BaseModel):
    id: str
    title: str
    priority: str
    dueDate: str
    status: str

class CreateTaskRequest(BaseModel):
    title: str = Field(min_length=1)
    priority: str = Field(pattern='^(high|medium|low)$')
    dueDate: str

class RestockRecommendation(BaseModel):
    item_sku: str
    item_name: str
    trend: str
    demand_gap: int
    recommended_quantity: int
    unit_cost: float
    line_total: float
    lead_time_days: int

class RestockRecommendationResponse(BaseModel):
    budget: float
    total_cost: float
    remaining_budget: float
    items: List[RestockRecommendation]

class RestockOrderLineRequest(BaseModel):
    item_sku: str
    quantity: int = Field(gt=0)

class CreateRestockOrderRequest(BaseModel):
    budget: float = Field(gt=0)
    items: List[RestockOrderLineRequest] = Field(min_length=1)

class RestockOrderLine(BaseModel):
    item_sku: str
    item_name: str
    quantity: int
    unit_cost: float
    line_total: float
    lead_time_days: int

class RestockOrder(BaseModel):
    id: str
    order_number: str
    status: str
    budget: float
    total_cost: float
    items: List[RestockOrderLine]
    lead_time_days: int
    submitted_date: str
    expected_delivery_date: str

# Submitted restocking orders live only in process memory, like the rest of the mock data
restock_orders: List[dict] = []

# Tasks created from the profile menu; also in-memory only. The client merges these with
# its own mock tasks (integer ids), so these use "TASK-n" string ids to avoid collisions.
tasks: List[dict] = []
# Monotonic counter rather than len(tasks) + 1, which would reuse an id after a delete
task_ids = count(1)

# Increasing demand is restocked before stable/decreasing demand
TREND_PRIORITY = {'increasing': 0, 'stable': 1, 'decreasing': 2}

def recommend_restock(budget: float) -> dict:
    """Pick forecast items to restock within a budget.

    Items with no positive demand gap (forecasted minus current) need no restock.
    The rest are ranked by trend, then by largest gap, and filled greedily; when an
    item no longer fits in full, the remaining budget buys as many units as it can.
    """
    candidates = []
    for forecast in demand_forecasts:
        gap = forecast['forecasted_demand'] - forecast['current_demand']
        if gap > 0:
            candidates.append((forecast, gap))
    candidates.sort(key=lambda c: (TREND_PRIORITY.get(c[0]['trend'], 99), -c[1]))

    remaining = budget
    items = []
    for forecast, gap in candidates:
        unit_cost = forecast['unit_cost']
        # Floor division so the partial line never exceeds the remaining budget
        quantity = min(gap, int(remaining // unit_cost))
        if quantity <= 0:
            # Keep scanning: a cheaper item further down may still fit
            continue
        line_total = round(quantity * unit_cost, 2)
        remaining = round(remaining - line_total, 2)
        items.append({
            'item_sku': forecast['item_sku'],
            'item_name': forecast['item_name'],
            'trend': forecast['trend'],
            'demand_gap': gap,
            'recommended_quantity': quantity,
            'unit_cost': unit_cost,
            'line_total': line_total,
            'lead_time_days': forecast['lead_time_days'],
        })

    return {
        'budget': budget,
        'total_cost': round(budget - remaining, 2),
        'remaining_budget': remaining,
        'items': items,
    }

# API endpoints
@app.get("/")
def root():
    return {"message": "Factory Inventory Management System API", "version": "1.0.0"}

@app.get("/api/inventory", response_model=List[InventoryItem])
def get_inventory(
    warehouse: Optional[str] = None,
    category: Optional[str] = None
):
    """Get all inventory items with optional filtering"""
    return apply_filters(inventory_items, warehouse, category)

@app.get("/api/inventory/{item_id}", response_model=InventoryItem)
def get_inventory_item(item_id: str):
    """Get a specific inventory item"""
    item = next((item for item in inventory_items if item["id"] == item_id), None)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    return item

@app.get("/api/orders", response_model=List[Order])
def get_orders(
    warehouse: Optional[str] = None,
    category: Optional[str] = None,
    status: Optional[str] = None,
    month: Optional[str] = None
):
    """Get all orders with optional filtering"""
    filtered_orders = apply_filters(orders, warehouse, category, status)
    filtered_orders = filter_by_month(filtered_orders, month)
    return filtered_orders

@app.get("/api/orders/{order_id}", response_model=Order)
def get_order(order_id: str):
    """Get a specific order"""
    order = next((order for order in orders if order["id"] == order_id), None)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return order

@app.get("/api/demand", response_model=List[DemandForecast])
def get_demand_forecasts():
    """Get demand forecasts"""
    return demand_forecasts

@app.get("/api/restocking/recommendations", response_model=RestockRecommendationResponse)
def get_restock_recommendations(budget: float = Query(gt=0)):
    """Recommend forecast items to restock within the given budget"""
    return recommend_restock(budget)

@app.get("/api/restocking/orders", response_model=List[RestockOrder])
def get_restock_orders():
    """Get submitted restocking orders, newest first"""
    return list(reversed(restock_orders))

@app.post("/api/restocking/orders", response_model=RestockOrder, status_code=201)
def create_restock_order(request: CreateRestockOrderRequest):
    """Submit a restocking order"""
    forecasts_by_sku = {f['item_sku']: f for f in demand_forecasts}

    lines = []
    for line in request.items:
        forecast = forecasts_by_sku.get(line.item_sku)
        if not forecast:
            raise HTTPException(status_code=400, detail=f"Unknown SKU {line.item_sku}")
        # Prices come from server data, never from the client
        lines.append({
            'item_sku': forecast['item_sku'],
            'item_name': forecast['item_name'],
            'quantity': line.quantity,
            'unit_cost': forecast['unit_cost'],
            'line_total': round(line.quantity * forecast['unit_cost'], 2),
            'lead_time_days': forecast['lead_time_days'],
        })

    total_cost = round(sum(l['line_total'] for l in lines), 2)
    if total_cost > request.budget:
        raise HTTPException(
            status_code=400,
            detail=f"Order total {total_cost} exceeds budget {request.budget}"
        )

    # The order arrives in one shipment, so it waits for its slowest item
    lead_time_days = max(l['lead_time_days'] for l in lines)
    submitted = datetime.now()
    order_index = len(restock_orders) + 1
    order = {
        'id': f"RST-{order_index}",
        'order_number': f"RST-{submitted.year}-{order_index:04d}",
        'status': 'Submitted',
        'budget': request.budget,
        'total_cost': total_cost,
        'items': lines,
        'lead_time_days': lead_time_days,
        'submitted_date': submitted.isoformat(timespec='seconds'),
        'expected_delivery_date': (submitted + timedelta(days=lead_time_days)).date().isoformat(),
    }
    restock_orders.append(order)
    return order

@app.get("/api/tasks", response_model=List[Task])
def get_tasks():
    """Get tasks, newest first (the client prepends new tasks, so this keeps the order stable)"""
    return list(reversed(tasks))

@app.post("/api/tasks", response_model=Task, status_code=201)
def create_task(request: CreateTaskRequest):
    """Create a pending task"""
    task = {
        'id': f"TASK-{next(task_ids)}",
        'title': request.title.strip(),
        'priority': request.priority,
        'dueDate': request.dueDate,
        'status': 'pending',
    }
    tasks.append(task)
    return task

def find_task(task_id: str) -> dict:
    task = next((t for t in tasks if t['id'] == task_id), None)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    return task

@app.delete("/api/tasks/{task_id}")
def delete_task(task_id: str):
    """Delete a task"""
    tasks.remove(find_task(task_id))
    return {"deleted": task_id}

@app.patch("/api/tasks/{task_id}", response_model=Task)
def toggle_task(task_id: str):
    """Toggle a task between pending and completed"""
    task = find_task(task_id)
    task['status'] = 'completed' if task['status'] == 'pending' else 'pending'
    return task

@app.post("/api/purchase-orders", response_model=PurchaseOrder, status_code=201)
def create_purchase_order(request: CreatePurchaseOrderRequest):
    """Create a purchase order for a backlog item (one per item)"""
    if not any(b['id'] == request.backlog_item_id for b in backlog_items):
        raise HTTPException(status_code=404, detail=f"Backlog item {request.backlog_item_id} not found")
    if any(po['backlog_item_id'] == request.backlog_item_id for po in purchase_orders):
        raise HTTPException(status_code=400, detail=f"Backlog item {request.backlog_item_id} already has a purchase order")

    po = {
        **request.model_dump(),
        'id': f"PO-{len(purchase_orders) + 1:04d}",
        'status': 'Pending',
        'created_date': datetime.now().isoformat(timespec='seconds'),
    }
    # Appending to the shared list makes /api/backlog report has_purchase_order for this item
    purchase_orders.append(po)
    return po

@app.get("/api/purchase-orders/{backlog_item_id}", response_model=PurchaseOrder)
def get_purchase_order_by_backlog_item(backlog_item_id: str):
    """Get the purchase order raised for a backlog item"""
    po = next((p for p in purchase_orders if p['backlog_item_id'] == backlog_item_id), None)
    if not po:
        raise HTTPException(status_code=404, detail=f"No purchase order found for backlog item {backlog_item_id}")
    return po

@app.get("/api/backlog", response_model=List[BacklogItem])
def get_backlog():
    """Get backlog items with purchase order status"""
    # Add has_purchase_order flag to each backlog item
    result = []
    for item in backlog_items:
        item_dict = dict(item)
        # Check if this backlog item has a purchase order
        has_po = any(po["backlog_item_id"] == item["id"] for po in purchase_orders)
        item_dict["has_purchase_order"] = has_po
        result.append(item_dict)
    return result

@app.get("/api/dashboard/summary")
def get_dashboard_summary(
    warehouse: Optional[str] = None,
    category: Optional[str] = None,
    status: Optional[str] = None,
    month: Optional[str] = None
):
    """Get summary statistics for dashboard with optional filtering"""
    # Filter inventory
    filtered_inventory = apply_filters(inventory_items, warehouse, category)

    # Filter orders
    filtered_orders = apply_filters(orders, warehouse, category, status)
    filtered_orders = filter_by_month(filtered_orders, month)

    total_inventory_value = sum(item["quantity_on_hand"] * item["unit_cost"] for item in filtered_inventory)
    low_stock_items = len([item for item in filtered_inventory if item["quantity_on_hand"] <= item["reorder_point"]])
    pending_orders = len([order for order in filtered_orders if order["status"] in ["Processing", "Backordered"]])
    total_backlog_items = len(backlog_items)

    return {
        "total_inventory_value": round(total_inventory_value, 2),
        "low_stock_items": low_stock_items,
        "pending_orders": pending_orders,
        "total_backlog_items": total_backlog_items,
        "total_orders_value": sum(order["total_value"] for order in filtered_orders)
    }

@app.get("/api/spending/summary")
def get_spending_summary():
    """Get spending summary statistics"""
    return spending_summary

@app.get("/api/spending/monthly")
def get_monthly_spending():
    """Get monthly spending breakdown"""
    return monthly_spending

@app.get("/api/spending/categories")
def get_category_spending():
    """Get spending by category"""
    # Derive each share from the amounts so the percentages always sum to ~100%,
    # instead of trusting the stored values (which had drifted to 123.6% in total)
    total = sum(c['amount'] for c in category_spending)
    return [
        {**c, 'percentage': round(c['amount'] / total * 100, 1) if total else 0}
        for c in category_spending
    ]

@app.get("/api/spending/transactions")
def get_recent_transactions():
    """Get recent transactions"""
    return recent_transactions

@app.get("/api/reports/quarterly")
def get_quarterly_reports():
    """Get quarterly performance reports"""
    # Calculate quarterly statistics from orders
    quarters = {}

    for order in orders:
        order_date = order.get('order_date', '')
        # Determine quarter
        if '2025-01' in order_date or '2025-02' in order_date or '2025-03' in order_date:
            quarter = 'Q1-2025'
        elif '2025-04' in order_date or '2025-05' in order_date or '2025-06' in order_date:
            quarter = 'Q2-2025'
        elif '2025-07' in order_date or '2025-08' in order_date or '2025-09' in order_date:
            quarter = 'Q3-2025'
        elif '2025-10' in order_date or '2025-11' in order_date or '2025-12' in order_date:
            quarter = 'Q4-2025'
        else:
            continue

        if quarter not in quarters:
            quarters[quarter] = {
                'quarter': quarter,
                'total_orders': 0,
                'total_revenue': 0,
                'delivered_orders': 0,
                'avg_order_value': 0
            }

        quarters[quarter]['total_orders'] += 1
        quarters[quarter]['total_revenue'] += order.get('total_value', 0)
        if order.get('status') == 'Delivered':
            quarters[quarter]['delivered_orders'] += 1

    # Calculate averages and fulfillment rate
    result = []
    for q, data in quarters.items():
        if data['total_orders'] > 0:
            data['avg_order_value'] = round(data['total_revenue'] / data['total_orders'], 2)
            data['fulfillment_rate'] = round((data['delivered_orders'] / data['total_orders']) * 100, 1)
        result.append(data)

    # Sort by quarter
    result.sort(key=lambda x: x['quarter'])
    return result

@app.get("/api/reports/monthly-trends")
def get_monthly_trends():
    """Get month-over-month trends"""
    months = {}

    for order in orders:
        order_date = order.get('order_date', '')
        if not order_date:
            continue

        # Extract month (format: YYYY-MM-DD)
        month = order_date[:7]  # Gets YYYY-MM

        if month not in months:
            months[month] = {
                'month': month,
                'order_count': 0,
                'revenue': 0,
                'delivered_count': 0
            }

        months[month]['order_count'] += 1
        months[month]['revenue'] += order.get('total_value', 0)
        if order.get('status') == 'Delivered':
            months[month]['delivered_count'] += 1

    # Convert to list and sort
    result = list(months.values())
    result.sort(key=lambda x: x['month'])
    return result

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
