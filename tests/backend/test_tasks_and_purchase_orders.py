"""
Tests for task and purchase order API endpoints, plus data-loading fixes.
"""
import pytest

import main


@pytest.fixture(autouse=True)
def reset_state():
    """Tasks and purchase orders are module-level state, so isolate each test."""
    po_snapshot = list(main.purchase_orders)
    main.tasks.clear()
    yield
    main.tasks.clear()
    main.purchase_orders[:] = po_snapshot


def new_task(client, **overrides):
    payload = {"title": "Check stock", "priority": "high", "dueDate": "2026-11-10", **overrides}
    return client.post("/api/tasks", json=payload)


def first_backlog_id(client):
    return client.get("/api/backlog").json()[0]["id"]


def new_po(client, backlog_item_id, **overrides):
    payload = {
        "backlog_item_id": backlog_item_id,
        "supplier_name": "FilterMax Inc",
        "quantity": 350,
        "unit_cost": 12.4,
        "expected_delivery_date": "2026-11-20",
        **overrides,
    }
    return client.post("/api/purchase-orders", json=payload)


class TestTaskEndpoints:
    """Test suite for task endpoints."""

    def test_get_all_tasks_initially_empty(self, client):
        """Test that no tasks exist before one is created."""
        response = client.get("/api/tasks")
        assert response.status_code == 200
        assert response.json() == []

    def test_create_task(self, client):
        """Test creating a task returns it as pending with a string id."""
        response = new_task(client, title="  Review Q4 levels  ")
        assert response.status_code == 201

        task = response.json()
        assert task["id"].startswith("TASK-")
        assert task["title"] == "Review Q4 levels"
        assert task["priority"] == "high"
        assert task["dueDate"] == "2026-11-10"
        assert task["status"] == "pending"

    def test_tasks_listed_newest_first(self, client):
        """Test that the newest task comes first."""
        new_task(client, title="First")
        new_task(client, title="Second")

        titles = [t["title"] for t in client.get("/api/tasks").json()]
        assert titles == ["Second", "First"]

    def test_toggle_task(self, client):
        """Test that PATCH flips status between pending and completed."""
        task_id = new_task(client).json()["id"]

        assert client.patch(f"/api/tasks/{task_id}").json()["status"] == "completed"
        assert client.patch(f"/api/tasks/{task_id}").json()["status"] == "pending"

    def test_delete_task(self, client):
        """Test deleting a task removes it."""
        task_id = new_task(client).json()["id"]

        assert client.delete(f"/api/tasks/{task_id}").status_code == 200
        assert client.get("/api/tasks").json() == []

    def test_task_ids_not_reused_after_delete(self, client):
        """Test that a new task never gets the id of a deleted one."""
        first = new_task(client).json()["id"]
        client.delete(f"/api/tasks/{first}")

        assert new_task(client).json()["id"] != first

    def test_nonexistent_task(self, client):
        """Test that toggling or deleting an unknown task returns 404."""
        for response in [client.patch("/api/tasks/TASK-999"), client.delete("/api/tasks/TASK-999")]:
            assert response.status_code == 404
            assert "not found" in response.json()["detail"].lower()

    def test_create_task_invalid_payload(self, client):
        """Test that an empty title or unknown priority fails validation."""
        assert new_task(client, title="").status_code == 422
        assert new_task(client, priority="urgent").status_code == 422


class TestPurchaseOrderEndpoints:
    """Test suite for purchase order endpoints."""

    def test_create_purchase_order(self, client):
        """Test creating a purchase order for a backlog item."""
        backlog_id = first_backlog_id(client)
        response = new_po(client, backlog_id, notes="Rush")
        assert response.status_code == 201

        po = response.json()
        assert po["id"].startswith("PO-")
        assert po["backlog_item_id"] == backlog_id
        assert po["status"] == "Pending"
        assert po["quantity"] == 350
        assert po["notes"] == "Rush"
        assert "created_date" in po

    def test_get_purchase_order_by_backlog_item(self, client):
        """Test fetching the PO that was created for a backlog item."""
        backlog_id = first_backlog_id(client)
        created = new_po(client, backlog_id).json()

        response = client.get(f"/api/purchase-orders/{backlog_id}")
        assert response.status_code == 200
        assert response.json() == created

    def test_backlog_reflects_purchase_order(self, client):
        """Test that /api/backlog flags the item once a PO exists."""
        backlog_id = first_backlog_id(client)
        new_po(client, backlog_id)

        item = next(b for b in client.get("/api/backlog").json() if b["id"] == backlog_id)
        assert item["has_purchase_order"] is True

    def test_duplicate_purchase_order_rejected(self, client):
        """Test that a backlog item can only have one PO."""
        backlog_id = first_backlog_id(client)
        new_po(client, backlog_id)

        response = new_po(client, backlog_id)
        assert response.status_code == 400
        assert "already has" in response.json()["detail"]

    def test_get_nonexistent_purchase_order(self, client):
        """Test that a backlog item without a PO returns 404."""
        response = client.get("/api/purchase-orders/nonexistent-999")
        assert response.status_code == 404
        assert "detail" in response.json()

    def test_purchase_order_unknown_backlog_item(self, client):
        """Test that a PO for an unknown backlog item returns 404."""
        response = new_po(client, "nonexistent-999")
        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    def test_create_purchase_order_invalid_payload(self, client):
        """Test that empty supplier and non-positive quantity/cost fail validation."""
        backlog_id = first_backlog_id(client)
        assert new_po(client, backlog_id, supplier_name="").status_code == 422
        assert new_po(client, backlog_id, quantity=0).status_code == 422
        assert new_po(client, backlog_id, unit_cost=-1).status_code == 422


class TestDataFixes:
    """Regression tests for data loading and derived values."""

    def test_category_spending_percentages_sum_to_100(self, client):
        """Test that category shares are derived from amounts and add up."""
        data = client.get("/api/spending/categories").json()
        total = sum(c["amount"] for c in data)

        assert abs(sum(c["percentage"] for c in data) - 100) < 0.5
        for category in data:
            assert abs(category["percentage"] - category["amount"] / total * 100) < 0.1

    def test_inventory_names_decoded_as_utf8(self, client):
        """Test that non-ASCII characters in data files are not mojibake."""
        names = [item["name"] for item in client.get("/api/inventory").json()]

        assert "Dual Output ±15V Power Supply" in names
        assert not any("Â" in name for name in names)
