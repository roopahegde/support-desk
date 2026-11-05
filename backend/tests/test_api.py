"""API tests: real HTTP-style requests through TestClient, against a temporary database."""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from backend.app.db import connect, init_db
from backend.app.main import app, get_db
from backend.app.seed import seed

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
TEST_API_KEY = "test-key"  # not a real secret: it only exists while a test runs


@pytest.fixture
def db_path(tmp_path):
    """A fresh, seeded database file per test."""
    path = tmp_path / "test.db"
    conn = connect(path)
    init_db(conn)
    seed(conn, now=NOW)
    conn.close()
    return path


@pytest.fixture
def anonymous_client(db_path, monkeypatch):
    """A client that sends no API key, using the temporary database, never support_desk.db."""
    # monkeypatch undoes this after the test, and the tests never depend on a .env file.
    monkeypatch.setenv("BACKEND_API_KEY", TEST_API_KEY)

    def get_test_db():
        conn = connect(db_path)
        try:
            yield conn
        finally:
            conn.close()

    app.dependency_overrides[get_db] = get_test_db
    yield TestClient(app)
    app.dependency_overrides.clear()  # don't leak the override into other tests


@pytest.fixture
def client(anonymous_client):
    """The same client, sending the correct API key on every request."""
    anonymous_client.headers["X-API-Key"] = TEST_API_KEY
    return anonymous_client


def run_sql(db_path, sql):
    """Add data the seed doesn't have, to set up an edge case."""
    conn = connect(db_path)
    with conn:  # commits on success
        conn.execute(sql)
    conn.close()


# --- GET /orders/{id} ---


def test_get_order_returns_the_order_with_its_items(client):
    response = client.get("/orders/1042")
    assert response.status_code == 200
    assert response.json() == {
        "id": 1042,
        "customer_id": 1,
        "status": "delivered",
        "total_paise": 349_900,
        "placed_at": "2026-09-22T12:00:00Z",
        "shipped_at": "2026-09-24T12:00:00Z",
        "delivered_at": "2026-09-26T12:00:00Z",
        "items": [{"sku": "SPK-200", "quantity": 1, "unit_price_paise": 349_900}],
    }


def test_get_order_not_yet_shipped_has_null_dates(client):
    order = client.get("/orders/1048").json()
    assert order["shipped_at"] is None
    assert order["delivered_at"] is None
    assert order["items"][0]["quantity"] == 2


def test_get_order_unknown_id_returns_404(client):
    response = client.get("/orders/9999")
    assert response.status_code == 404
    assert response.json() == {"detail": "Order 9999 not found"}


def test_get_order_non_integer_id_returns_422(client):
    assert client.get("/orders/abc").status_code == 422


# --- GET /customers/{id}/orders ---


def test_customer_orders_are_newest_first_with_their_own_items(client):
    response = client.get("/customers/1/orders")
    assert response.status_code == 200
    summary = [(order["id"], [item["sku"] for item in order["items"]]) for order in response.json()]
    assert summary == [
        (1048, ["TSH-101"]),
        (1042, ["SPK-200"]),
        (1043, ["HPH-310"]),
    ]


def test_customer_order_with_two_items_is_listed_once(client, db_path):
    run_sql(
        db_path,
        "INSERT INTO order_items (order_id, sku, quantity, unit_price_paise)"
        " VALUES (1042, 'BAG-220', 1, 249900)",
    )
    orders = client.get("/customers/1/orders").json()
    matching = [order for order in orders if order["id"] == 1042]
    assert len(matching) == 1
    assert [item["sku"] for item in matching[0]["items"]] == ["SPK-200", "BAG-220"]


def test_customer_order_without_items_is_kept(client, db_path):
    run_sql(
        db_path,
        "INSERT INTO orders (id, customer_id, status, total_paise, placed_at)"
        " VALUES (7777, 1, 'placed', 0, '2026-10-01T00:00:00+00:00')",
    )
    orders = {order["id"]: order for order in client.get("/customers/1/orders").json()}
    assert orders[7777]["items"] == []


def test_customer_without_orders_gets_an_empty_list(client, db_path):
    run_sql(db_path, "INSERT INTO customers VALUES (4, 'No Orders', 'none@example.com')")
    response = client.get("/customers/4/orders")
    assert response.status_code == 200
    assert response.json() == []


def test_unknown_customer_returns_404(client):
    response = client.get("/customers/999/orders")
    assert response.status_code == 404
    assert response.json() == {"detail": "Customer 999 not found"}


def test_customer_non_integer_id_returns_422(client):
    assert client.get("/customers/abc/orders").status_code == 422


# --- GET /products/{sku}/stock ---


def test_product_stock_returns_the_current_level(client):
    response = client.get("/products/SPK-200/stock")
    assert response.status_code == 200
    assert response.json() == {"sku": "SPK-200", "name": "Bluetooth Speaker", "stock": 25}


def test_out_of_stock_product_is_not_an_error(client):
    response = client.get("/products/KTL-500/stock")
    assert response.status_code == 200
    assert response.json()["stock"] == 0


def test_unknown_product_returns_404(client):
    response = client.get("/products/NOPE-1/stock")
    assert response.status_code == 404
    assert response.json() == {"detail": "Product NOPE-1 not found"}


# --- GET /policies/refund ---


def test_refund_policy_publishes_the_business_rules(client):
    response = client.get("/policies/refund")
    assert response.status_code == 200
    assert response.json() == {
        "refund_window_days": 30,
        "changed_mind_window_days": 7,
        "accepted_reasons": ["damaged", "wrong_item", "not_as_described", "changed_mind"],
        "approval_threshold_paise": 1_000_000,
        "max_refunds_per_order": 1,
    }


# --- POST /orders/{id}/refunds ---

VALID_REFUND = {"reason": "damaged", "amount_paise": 349_900}


def test_create_refund_returns_201_with_the_refund(client):
    response = client.post("/orders/1042/refunds", json=VALID_REFUND)
    assert response.status_code == 201
    refund = response.json()
    assert refund["order_id"] == 1042
    assert refund["amount_paise"] == 349_900
    assert refund["reason"] == "damaged"
    assert refund["status"] == "completed"
    assert "idempotency_key" not in refund  # internal: not part of the response model


def test_create_refund_marks_the_order_as_refunded(client):
    client.post("/orders/1042/refunds", json=VALID_REFUND)
    assert client.get("/orders/1042").json()["status"] == "refunded"


def test_second_refund_for_the_same_order_returns_409(client, db_path):
    client.post("/orders/1042/refunds", json=VALID_REFUND)
    response = client.post("/orders/1042/refunds", json=VALID_REFUND)
    assert response.status_code == 409
    assert response.json() == {"detail": "Order 1042 has already been refunded"}

    conn = connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM refunds WHERE order_id = 1042").fetchone()[0]
    conn.close()
    assert count == 1


def test_refund_of_seeded_refunded_order_returns_409(client):
    response = client.post("/orders/1045/refunds", json=VALID_REFUND)
    assert response.status_code == 409


def test_refund_of_unknown_order_returns_404(client):
    response = client.post("/orders/9999/refunds", json=VALID_REFUND)
    assert response.status_code == 404
    assert response.json() == {"detail": "Order 9999 not found"}


@pytest.mark.parametrize(
    "body",
    [
        {"reason": "bored", "amount_paise": 100},  # reason not accepted
        {"reason": "damaged", "amount_paise": 0},  # amount must be above zero
        {"reason": "damaged", "amount_paise": -100},
        {"reason": "damaged", "amount_paise": "lots"},  # not a number
        {"reason": "damaged"},  # amount missing
        {"amount_paise": 100},  # reason missing
    ],
)
def test_invalid_refund_body_returns_422_and_changes_nothing(client, body):
    response = client.post("/orders/1042/refunds", json=body)
    assert response.status_code == 422
    assert client.get("/orders/1042").json()["status"] == "delivered"


def test_failed_status_update_rolls_back_the_refund(client, db_path):
    # Make the second write fail: a database trigger rejects the order's status change.
    run_sql(
        db_path,
        "CREATE TRIGGER block_update BEFORE UPDATE ON orders"
        " BEGIN SELECT RAISE(ABORT, 'simulated failure'); END",
    )
    # raise_server_exceptions=False: see the 500 a real client would get, not the Python error.
    response = TestClient(
        app, raise_server_exceptions=False, headers={"X-API-Key": TEST_API_KEY}
    ).post("/orders/1042/refunds", json=VALID_REFUND)
    assert response.status_code == 500  # a server failure must not look like "already refunded"

    conn = connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM refunds WHERE order_id = 1042").fetchone()[0]
    conn.close()
    assert count == 0  # the INSERT was undone


# --- API-key auth ---


def test_correct_api_key_is_accepted(client):
    assert client.get("/orders/1042").status_code == 200


def test_missing_api_key_returns_401(anonymous_client):
    response = anonymous_client.get("/orders/1042")
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid API key"}


def test_wrong_api_key_gets_the_same_401_as_a_missing_one(anonymous_client):
    response = anonymous_client.get("/orders/1042", headers={"X-API-Key": "wrong"})
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid API key"}


@pytest.mark.parametrize(
    "path",
    [
        "/orders/1042",
        "/customers/1/orders",
        "/products/SPK-200/stock",
        "/policies/refund",
    ],
)
def test_every_read_endpoint_requires_a_key(anonymous_client, path):
    assert anonymous_client.get(path).status_code == 401


def test_refund_without_a_key_is_rejected_and_changes_nothing(anonymous_client):
    response = anonymous_client.post("/orders/1042/refunds", json=VALID_REFUND)
    assert response.status_code == 401

    order = anonymous_client.get("/orders/1042", headers={"X-API-Key": TEST_API_KEY}).json()
    assert order["status"] == "delivered"


def test_invalid_id_without_a_key_gets_401_not_validation_details(anonymous_client):
    assert anonymous_client.get("/orders/abc").status_code == 401


def test_server_with_no_key_configured_rejects_requests(client, monkeypatch):
    monkeypatch.delenv("BACKEND_API_KEY")
    response = client.get("/orders/1042")
    assert response.status_code == 500
    assert TEST_API_KEY not in response.text


def test_server_with_an_empty_key_never_authenticates(anonymous_client, monkeypatch):
    monkeypatch.setenv("BACKEND_API_KEY", "")
    assert anonymous_client.get("/orders/1042").status_code != 200
    assert anonymous_client.get("/orders/1042", headers={"X-API-Key": ""}).status_code != 200


def test_docs_stay_public(anonymous_client):
    assert anonymous_client.get("/docs").status_code == 200
    assert anonymous_client.get("/openapi.json").status_code == 200


def test_swagger_advertises_the_api_key_header(anonymous_client):
    schemes = anonymous_client.get("/openapi.json").json()["components"]["securitySchemes"]
    assert schemes["APIKeyHeader"] == {"type": "apiKey", "in": "header", "name": "X-API-Key"}
