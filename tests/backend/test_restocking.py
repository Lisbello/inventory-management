"""
Tests for restocking API endpoints.
"""
from datetime import date, timedelta

import pytest

import main


@pytest.fixture(autouse=True)
def reset_restock_orders():
    """Submitted orders are module-level state, so isolate each test."""
    main.restock_orders.clear()
    yield
    main.restock_orders.clear()


def get_forecasts(client):
    return {f["item_sku"]: f for f in client.get("/api/demand").json()}


class TestRestockRecommendations:
    """Test suite for the restocking recommendation endpoint."""

    def test_demand_forecast_has_cost_and_lead_time(self, client):
        """Test that forecasts expose the fields restocking depends on."""
        for forecast in get_forecasts(client).values():
            assert isinstance(forecast["unit_cost"], (int, float))
            assert forecast["unit_cost"] > 0
            assert isinstance(forecast["lead_time_days"], int)
            assert forecast["lead_time_days"] > 0

    def test_get_recommendations_structure(self, client):
        """Test the recommendation response structure."""
        response = client.get("/api/restocking/recommendations?budget=5000")
        assert response.status_code == 200

        data = response.json()
        assert data["budget"] == 5000
        assert isinstance(data["items"], list)
        assert len(data["items"]) > 0

        for item in data["items"]:
            for field in ["item_sku", "item_name", "trend", "demand_gap", "recommended_quantity",
                          "unit_cost", "line_total", "lead_time_days"]:
                assert field in item

    def test_recommendations_stay_within_budget(self, client):
        """Test that totals never exceed the budget and add up."""
        for budget in [50, 1000, 5000, 20000, 1000000]:
            data = client.get(f"/api/restocking/recommendations?budget={budget}").json()

            calculated_total = sum(item["line_total"] for item in data["items"])
            assert abs(data["total_cost"] - calculated_total) < 0.01
            assert data["total_cost"] <= budget
            assert abs(data["remaining_budget"] - (budget - data["total_cost"])) < 0.01

    def test_recommendation_quantity_calculation(self, client):
        """Test that quantities are positive, capped by the demand gap, and priced from data."""
        forecasts = get_forecasts(client)
        data = client.get("/api/restocking/recommendations?budget=5000").json()

        for item in data["items"]:
            forecast = forecasts[item["item_sku"]]
            gap = forecast["forecasted_demand"] - forecast["current_demand"]
            assert item["demand_gap"] == gap
            assert 0 < item["recommended_quantity"] <= gap
            assert item["unit_cost"] == forecast["unit_cost"]
            assert abs(item["line_total"] - item["recommended_quantity"] * item["unit_cost"]) < 0.01

    def test_large_budget_covers_every_gap(self, client):
        """Test that an unlimited budget restocks every item with positive demand gap in full."""
        forecasts = get_forecasts(client)
        data = client.get("/api/restocking/recommendations?budget=10000000").json()

        expected_skus = {
            sku for sku, f in forecasts.items()
            if f["forecasted_demand"] > f["current_demand"]
        }
        assert {item["item_sku"] for item in data["items"]} == expected_skus
        for item in data["items"]:
            assert item["recommended_quantity"] == item["demand_gap"]

    def test_decreasing_demand_not_recommended(self, client):
        """Test that items without a positive demand gap are excluded."""
        forecasts = get_forecasts(client)
        data = client.get("/api/restocking/recommendations?budget=10000000").json()

        for item in data["items"]:
            forecast = forecasts[item["item_sku"]]
            assert forecast["forecasted_demand"] > forecast["current_demand"]

    def test_increasing_trend_ranked_first(self, client):
        """Test that increasing-demand items come before stable ones."""
        data = client.get("/api/restocking/recommendations?budget=10000000").json()
        trends = [item["trend"] for item in data["items"]]

        priority = {"increasing": 0, "stable": 1, "decreasing": 2}
        assert trends == sorted(trends, key=lambda t: priority[t])

    def test_recommendations_invalid_budget(self, client):
        """Test that zero, negative, and missing budgets are rejected."""
        assert client.get("/api/restocking/recommendations?budget=0").status_code == 422
        assert client.get("/api/restocking/recommendations?budget=-10").status_code == 422
        assert client.get("/api/restocking/recommendations").status_code == 422


class TestRestockOrders:
    """Test suite for submitting and listing restocking orders."""

    def test_get_restock_orders_initially_empty(self, client):
        """Test that no orders exist before one is submitted."""
        response = client.get("/api/restocking/orders")
        assert response.status_code == 200
        assert response.json() == []

    def test_create_restock_order(self, client):
        """Test submitting the recommended items as an order."""
        rec = client.get("/api/restocking/recommendations?budget=5000").json()
        payload = {
            "budget": 5000,
            "items": [
                {"item_sku": i["item_sku"], "quantity": i["recommended_quantity"]}
                for i in rec["items"]
            ],
        }

        response = client.post("/api/restocking/orders", json=payload)
        assert response.status_code == 201

        order = response.json()
        assert order["status"] == "Submitted"
        assert order["order_number"].startswith("RST-")
        assert abs(order["total_cost"] - rec["total_cost"]) < 0.01
        assert len(order["items"]) == len(rec["items"])

    def test_restock_order_lead_time_calculation(self, client):
        """Test that lead time is the slowest item's and drives the delivery date."""
        forecasts = get_forecasts(client)
        skus = list(forecasts)[:3]
        payload = {"budget": 1000000, "items": [{"item_sku": s, "quantity": 1} for s in skus]}

        order = client.post("/api/restocking/orders", json=payload).json()

        expected_lead = max(forecasts[s]["lead_time_days"] for s in skus)
        assert order["lead_time_days"] == expected_lead
        submitted = date.fromisoformat(order["submitted_date"][:10])
        assert date.fromisoformat(order["expected_delivery_date"]) == submitted + timedelta(days=expected_lead)

    def test_restock_order_uses_server_prices(self, client):
        """Test that line prices come from forecast data."""
        forecasts = get_forecasts(client)
        sku = next(iter(forecasts))
        payload = {"budget": 1000000, "items": [{"item_sku": sku, "quantity": 10}]}

        line = client.post("/api/restocking/orders", json=payload).json()["items"][0]
        assert line["unit_cost"] == forecasts[sku]["unit_cost"]
        assert abs(line["line_total"] - 10 * forecasts[sku]["unit_cost"]) < 0.01

    def test_submitted_order_listed_newest_first(self, client):
        """Test that submitted orders appear in the list, newest first."""
        sku = next(iter(get_forecasts(client)))
        for qty in [1, 2]:
            payload = {"budget": 1000000, "items": [{"item_sku": sku, "quantity": qty}]}
            assert client.post("/api/restocking/orders", json=payload).status_code == 201

        orders = client.get("/api/restocking/orders").json()
        assert len(orders) == 2
        assert orders[0]["items"][0]["quantity"] == 2
        assert orders[1]["items"][0]["quantity"] == 1

    def test_restock_order_over_budget(self, client):
        """Test that an order costing more than its budget is rejected."""
        sku = next(iter(get_forecasts(client)))
        payload = {"budget": 1, "items": [{"item_sku": sku, "quantity": 1000}]}

        response = client.post("/api/restocking/orders", json=payload)
        assert response.status_code == 400
        assert "exceeds budget" in response.json()["detail"]
        assert client.get("/api/restocking/orders").json() == []

    def test_restock_order_unknown_sku(self, client):
        """Test that an unknown SKU is rejected."""
        payload = {"budget": 1000, "items": [{"item_sku": "NOPE-999", "quantity": 1}]}

        response = client.post("/api/restocking/orders", json=payload)
        assert response.status_code == 400
        assert "NOPE-999" in response.json()["detail"]

    def test_restock_order_invalid_payload(self, client):
        """Test that empty items and non-positive quantities fail validation."""
        sku = next(iter(get_forecasts(client)))
        assert client.post("/api/restocking/orders", json={"budget": 1000, "items": []}).status_code == 422
        bad_qty = {"budget": 1000, "items": [{"item_sku": sku, "quantity": 0}]}
        assert client.post("/api/restocking/orders", json=bad_qty).status_code == 422
