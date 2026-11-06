"""API tests: real HTTP-style requests through TestClient, against a temporary database."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.db import connect, init_db
from backend.app.main import app, get_db, get_now
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
    app.dependency_overrides[get_now] = lambda: NOW  # the same "now" the seed dates are based on
    yield TestClient(app)
    app.dependency_overrides.clear()  # don't leak the overrides into other tests


@pytest.fixture
def client(anonymous_client):
    """The same client, sending the correct API key on every request."""
    anonymous_client.headers["X-API-Key"] = TEST_API_KEY
    return anonymous_client


def error(code, message):
    """The standard error body, as every endpoint returns it."""
    return {"error": {"code": code, "message": message}}


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
    assert response.json() == error("not_found", "Order 9999 not found")


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
    assert response.json() == error("not_found", "Customer 999 not found")


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
    assert response.json() == error("not_found", "Product NOPE-1 not found")


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


def post_refund(client, order_id=1042, body=VALID_REFUND, key="key-1"):
    """Send a refund request with an Idempotency-Key header."""
    return client.post(
        f"/orders/{order_id}/refunds", json=body, headers={"Idempotency-Key": key}
    )


def count_refunds(db_path, order_id):
    conn = connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM refunds WHERE order_id = ?", (order_id,)).fetchone()
    conn.close()
    return count[0]


def test_create_refund_returns_201_with_the_refund(client):
    response = post_refund(client)
    assert response.status_code == 201
    refund = response.json()
    assert refund["order_id"] == 1042
    assert refund["amount_paise"] == 349_900
    assert refund["reason"] == "damaged"
    assert refund["status"] == "completed"
    assert "idempotency_key" not in refund  # internal: not part of the response model


def test_create_refund_marks_the_order_as_refunded(client):
    post_refund(client)
    assert client.get("/orders/1042").json()["status"] == "refunded"


def test_second_refund_with_a_new_key_returns_409(client, db_path):
    post_refund(client, key="key-1")
    response = post_refund(client, key="key-2")
    assert response.status_code == 409
    assert response.json() == error("already_refunded", "Order 1042 has already been refunded")
    assert count_refunds(db_path, 1042) == 1


def test_refund_of_seeded_refunded_order_returns_409(client):
    response = post_refund(client, order_id=1045)
    assert response.status_code == 409


def test_refund_of_unknown_order_returns_404(client):
    response = post_refund(client, order_id=9999)
    assert response.status_code == 404
    assert response.json() == error("not_found", "Order 9999 not found")


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
    response = post_refund(client, body=body)
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
    response = post_refund(
        TestClient(app, raise_server_exceptions=False, headers={"X-API-Key": TEST_API_KEY})
    )
    assert response.status_code == 500  # a server failure must not look like "already refunded"
    assert response.json() == error(
        "internal_error", "An unexpected server error occurred. Please try again later."
    )
    assert count_refunds(db_path, 1042) == 0  # the INSERT was undone


# --- Refund rules (each rule is unit-tested in test_rules.py; these check the API wiring) ---


@pytest.mark.parametrize(
    ("order_id", "body", "message"),
    [
        (
            1043,
            VALID_REFUND,
            (
                "Order 1043 was delivered 45 days ago. Refunds are only allowed"
                " within 30 days of delivery."
            ),
        ),
        (
            1047,
            VALID_REFUND,
            (
                "Order 1047 cannot be refunded because it has not been delivered"
                " (its status is 'shipped')."
            ),
        ),
        (
            1048,
            VALID_REFUND,
            (
                "Order 1048 cannot be refunded because it has not been delivered"
                " (its status is 'placed')."
            ),
        ),
        (
            1049,
            {"reason": "changed_mind", "amount_paise": 249_900},
            (
                "Order 1049 was delivered 20 days ago. The reason 'changed_mind' is only"
                " accepted within 7 days of delivery."
            ),
        ),
        (
            1042,
            {"reason": "damaged", "amount_paise": 349_901},
            "The refund amount (349901 paise) is more than the order total (349900 paise).",
        ),
    ],
)
def test_refund_the_policy_forbids_returns_422_and_changes_nothing(
    client, db_path, order_id, body, message
):
    status_before = client.get(f"/orders/{order_id}").json()["status"]
    response = post_refund(client, order_id=order_id, body=body)
    assert response.status_code == 422
    assert response.json() == error("policy_violation", message)
    assert count_refunds(db_path, order_id) == 0
    assert client.get(f"/orders/{order_id}").json()["status"] == status_before


def test_damaged_refund_is_still_allowed_after_7_days(client):
    response = post_refund(client, order_id=1049, body={"reason": "damaged", "amount_paise": 249_900})
    assert response.status_code == 201
    assert response.json()["status"] == "completed"


def test_large_refund_is_saved_for_approval_without_refunding_the_order(client):
    body = {"reason": "damaged", "amount_paise": 5_499_900}
    response = post_refund(client, order_id=1044, body=body)
    assert response.status_code == 201
    assert response.json()["status"] == "pending_approval"
    assert client.get("/orders/1044").json()["status"] == "delivered"  # not refunded yet


def test_second_request_for_a_pending_refund_says_it_awaits_approval(client):
    body = {"reason": "damaged", "amount_paise": 5_499_900}
    post_refund(client, order_id=1044, body=body, key="key-1")
    response = post_refund(client, order_id=1044, body=body, key="key-2")
    assert response.status_code == 409
    assert response.json() == error("refund_pending_approval", "Order 1044 already has a refund waiting for human approval")


def test_retry_of_a_pending_refund_returns_it_unchanged(client):
    body = {"reason": "damaged", "amount_paise": 5_499_900}
    first = post_refund(client, order_id=1044, body=body)
    retry = post_refund(client, order_id=1044, body=body)
    assert retry.status_code == 201
    assert retry.json() == first.json()


def test_refund_uses_the_injected_clock_for_created_at(client):
    created_at = post_refund(client).json()["created_at"]
    assert created_at == "2026-10-01T12:00:00Z"


def test_retry_after_the_window_still_returns_the_original_refund(client, db_path):
    # The rules apply when a refund is created. A later retry is not a new refund.
    first = post_refund(client)
    app.dependency_overrides[get_now] = lambda: NOW + timedelta(days=60)
    retry = post_refund(client)
    assert retry.status_code == 201
    assert retry.json() == first.json()


# --- Idempotency-Key ---


def test_retry_with_the_same_key_returns_the_original_refund(client, db_path):
    first = post_refund(client)
    retry = post_refund(client)
    assert retry.status_code == 201
    assert retry.json() == first.json()
    assert count_refunds(db_path, 1042) == 1


def test_seeded_refund_can_be_replayed_with_its_key(client):
    body = {"reason": "damaged", "amount_paise": 129_900}
    response = post_refund(client, order_id=1045, body=body, key="seed-refund-1045")
    assert response.status_code == 201
    assert response.json()["order_id"] == 1045


@pytest.mark.parametrize(
    ("order_id", "body"),
    [
        (1042, {"reason": "damaged", "amount_paise": 100}),  # different amount
        (1042, {"reason": "wrong_item", "amount_paise": 349_900}),  # different reason
        (1046, VALID_REFUND),  # different order: keys are unique across all refunds
    ],
)
def test_reusing_a_key_for_a_different_request_returns_409(client, db_path, order_id, body):
    post_refund(client)
    response = post_refund(client, order_id=order_id, body=body)
    assert response.status_code == 409
    assert response.json() == error("idempotency_key_reused", "Idempotency key was already used for a different request")
    assert count_refunds(db_path, 1046) == 0  # the other order was not refunded


def test_missing_idempotency_key_returns_422(client, db_path):
    response = client.post("/orders/1042/refunds", json=VALID_REFUND)
    assert response.status_code == 422
    assert response.json()["error"]["fields"][0]["location"] == "header.idempotency-key"
    assert count_refunds(db_path, 1042) == 0


@pytest.mark.parametrize("key", ["", "k" * 256])
def test_empty_or_oversized_idempotency_key_returns_422(client, key):
    assert post_refund(client, key=key).status_code == 422


# A race can be lost at two points. Each test hides the checks the losing request ran
# before the other request committed, so those checks find nothing.
KEY_LOOKUP = "WHERE idempotency_key"
ORDER_REFUND_CHECK = "SELECT 1 FROM refunds WHERE order_id"


def test_retry_racing_past_the_key_lookup_returns_the_original_refund(client, db_path):
    first = post_refund(client)
    use_connection_that_misses_first_lookups(db_path, [KEY_LOOKUP])
    retry = post_refund(client)
    assert retry.status_code == 201
    assert retry.json() == first.json()
    assert count_refunds(db_path, 1042) == 1


def test_new_key_racing_past_the_key_lookup_returns_409(client, db_path):
    post_refund(client, key="key-1")
    use_connection_that_misses_first_lookups(db_path, [KEY_LOOKUP])
    response = post_refund(client, key="key-2")
    assert response.status_code == 409
    assert response.json() == error("already_refunded", "Order 1042 has already been refunded")
    assert count_refunds(db_path, 1042) == 1


# Racing all the way to the INSERT means the order was read before the other request
# committed, so it still looked 'delivered'. A pending refund leaves the order 'delivered',
# which reproduces that stale read with real data.
LARGE_REFUND = {"reason": "damaged", "amount_paise": 5_499_900}


def test_retry_racing_to_the_insert_returns_the_original_refund(client, db_path):
    first = post_refund(client, order_id=1044, body=LARGE_REFUND)
    use_connection_that_misses_first_lookups(db_path, [KEY_LOOKUP, ORDER_REFUND_CHECK])
    retry = post_refund(client, order_id=1044, body=LARGE_REFUND)
    assert retry.status_code == 201
    assert retry.json() == first.json()
    assert count_refunds(db_path, 1044) == 1


def test_new_key_racing_to_the_insert_returns_409(client, db_path):
    post_refund(client, order_id=1044, body=LARGE_REFUND, key="key-1")
    use_connection_that_misses_first_lookups(db_path, [KEY_LOOKUP, ORDER_REFUND_CHECK])
    response = post_refund(client, order_id=1044, body=LARGE_REFUND, key="key-2")
    assert response.status_code == 409
    assert response.json() == error("refund_pending_approval", "Order 1044 already has a refund waiting for human approval")
    assert count_refunds(db_path, 1044) == 1


def use_connection_that_misses_first_lookups(db_path, hidden):
    """Simulate a race: the first run of each hidden query finds nothing, as if it ran just
    before another request committed. Later runs of the same query see the real data."""

    class MissesFirstLookups:
        def __init__(self, conn):
            self.conn = conn
            self.missed = set()

        def execute(self, sql, params=()):
            for fragment in hidden:
                if fragment in sql and fragment not in self.missed:
                    self.missed.add(fragment)
                    return self.conn.execute("SELECT 1 WHERE 0")  # an empty result
            return self.conn.execute(sql, params)

        def __enter__(self):
            return self.conn.__enter__()

        def __exit__(self, *args):
            return self.conn.__exit__(*args)

    def get_racing_db():
        conn = connect(db_path)
        try:
            yield MissesFirstLookups(conn)
        finally:
            conn.close()

    app.dependency_overrides[get_db] = get_racing_db


# --- API-key auth ---


def test_correct_api_key_is_accepted(client):
    assert client.get("/orders/1042").status_code == 200


def test_missing_api_key_returns_401(anonymous_client):
    response = anonymous_client.get("/orders/1042")
    assert response.status_code == 401
    assert response.json() == error("unauthorized", "Invalid API key")


def test_wrong_api_key_gets_the_same_401_as_a_missing_one(anonymous_client):
    response = anonymous_client.get("/orders/1042", headers={"X-API-Key": "wrong"})
    assert response.status_code == 401
    assert response.json() == error("unauthorized", "Invalid API key")


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
    response = post_refund(anonymous_client)  # a valid request in every way except the API key
    assert response.status_code == 401

    order = anonymous_client.get("/orders/1042", headers={"X-API-Key": TEST_API_KEY}).json()
    assert order["status"] == "delivered"


def test_invalid_id_without_a_key_gets_401_not_validation_details(anonymous_client):
    assert anonymous_client.get("/orders/abc").status_code == 401


def test_server_with_no_key_configured_rejects_requests(client, monkeypatch):
    monkeypatch.delenv("BACKEND_API_KEY")
    response = client.get("/orders/1042")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "server_misconfigured"
    assert "not a problem with your API key" in response.json()["error"]["message"]
    assert TEST_API_KEY not in response.text


def test_missing_server_key_is_logged_for_the_operator(client, monkeypatch, caplog):
    monkeypatch.delenv("BACKEND_API_KEY")
    client.get("/orders/1042")
    assert "BACKEND_API_KEY is not set" in caplog.text  # caplog: pytest's capture of log output


def test_env_file_is_found_from_the_code_not_the_current_directory():
    from backend.app.main import ENV_FILE

    assert ENV_FILE == Path(__file__).resolve().parent.parent / ".env"  # backend/.env


def test_server_with_an_empty_key_is_misconfigured(anonymous_client, monkeypatch):
    monkeypatch.setenv("BACKEND_API_KEY", "")
    assert anonymous_client.get("/orders/1042").status_code == 500
    assert anonymous_client.get("/orders/1042", headers={"X-API-Key": ""}).status_code == 500


def test_non_ascii_api_key_gets_401_not_a_server_error(anonymous_client):
    # Bytes, so the test client sends the non-ASCII header exactly as an attacker could.
    headers = {"X-API-Key": "é".encode("latin-1")}
    assert anonymous_client.get("/orders/1042", headers=headers).status_code == 401


def test_docs_stay_public(anonymous_client):
    assert anonymous_client.get("/docs").status_code == 200
    assert anonymous_client.get("/openapi.json").status_code == 200


def test_swagger_advertises_the_api_key_header(anonymous_client):
    schemes = anonymous_client.get("/openapi.json").json()["components"]["securitySchemes"]
    assert schemes["APIKeyHeader"] == {"type": "apiKey", "in": "header", "name": "X-API-Key"}


# --- One error format everywhere ---


def test_validation_error_lists_each_field_with_a_readable_summary(client):
    response = post_refund(client, body={"reason": "bored", "amount_paise": 0})
    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == "validation_error"
    assert [field["location"] for field in body["fields"]] == ["body.reason", "body.amount_paise"]
    assert body["message"].startswith("The request is invalid: body.reason: Input should be")
    assert "body.amount_paise: Input should be greater than 0" in body["message"]


def test_invalid_path_parameter_uses_the_standard_format(client):
    body = client.get("/orders/abc").json()["error"]
    assert body["code"] == "validation_error"
    assert body["fields"][0]["location"] == "path.order_id"


def test_unknown_url_uses_the_standard_format(client):
    response = client.get("/no-such-endpoint")
    assert response.status_code == 404
    assert response.json() == error("not_found", "There is no endpoint at /no-such-endpoint.")


def test_wrong_http_method_uses_the_standard_format(client):
    response = client.delete("/orders/1042")
    assert response.status_code == 405
    assert response.json() == error("method_not_allowed", "DELETE is not allowed on /orders/1042.")


def test_unexpected_error_hides_internal_details(client, db_path):
    run_sql(db_path, "DROP TABLE order_items")  # make the endpoint crash
    response = TestClient(
        app, raise_server_exceptions=False, headers={"X-API-Key": TEST_API_KEY}
    ).get("/orders/1042")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "order_items" not in response.text  # no table names or stack trace leak out


def test_swagger_documents_the_standard_error_body(anonymous_client):
    spec = anonymous_client.get("/openapi.json").json()
    assert "HTTPValidationError" not in spec["components"]["schemas"]  # FastAPI's default is gone
    refund_responses = spec["paths"]["/orders/{order_id}/refunds"]["post"]["responses"]
    for status in ("401", "422", "500"):
        schema = refund_responses[status]["content"]["application/json"]["schema"]
        assert schema == {"$ref": "#/components/schemas/ErrorResponse"}
