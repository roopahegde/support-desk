"""Logging tests: where lines go, what each line says, and what must never be logged."""

import logging
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

import pytest
from fastapi.testclient import TestClient

from backend.app.db import connect, init_db
from backend.app.logging_config import BACKEND_DIR, configure_logging, resolve_log_file
from backend.app.main import app, get_db, get_now
from backend.app.seed import seed

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
TEST_API_KEY = "test-key-for-logging"  # distinctive, so a leak into the log is easy to spot


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    conn = connect(path)
    init_db(conn)
    seed(conn, now=NOW)
    conn.close()
    return path


@pytest.fixture
def client(db_path, monkeypatch):
    monkeypatch.setenv("BACKEND_API_KEYS", f"staff:test={TEST_API_KEY}")

    def get_test_db():
        conn = connect(db_path)
        try:
            yield conn
        finally:
            conn.close()

    app.dependency_overrides[get_db] = get_test_db
    app.dependency_overrides[get_now] = lambda: NOW
    yield TestClient(app, headers={"X-API-Key": TEST_API_KEY})
    app.dependency_overrides.clear()


@pytest.fixture
def log_file(tmp_path):
    """Send backend logs to a temporary file for one test, then back to the console."""
    path = tmp_path / "logs" / "backend.log"
    configure_logging(target="file", log_file=path, level="INFO")
    yield path
    configure_logging(target="console", level="INFO")  # also closes the file


def lines(path):
    return path.read_text(encoding="utf-8").splitlines()


def post_refund(client, order_id=1042, body=None, key="key-1"):
    body = body or {"reason": "damaged", "amount_paise": 349_900}
    return client.post(f"/orders/{order_id}/refunds", json=body, headers={"Idempotency-Key": key})


# --- Where lines go ---


def test_console_is_the_default(monkeypatch):
    monkeypatch.delenv("LOG_TARGET", raising=False)
    handler = configure_logging()
    assert isinstance(handler, logging.StreamHandler)
    assert not isinstance(handler, RotatingFileHandler)


def test_log_target_file_writes_to_a_rotating_file(monkeypatch, tmp_path):
    monkeypatch.setenv("LOG_TARGET", "file")
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "from-env.log"))
    handler = configure_logging()
    try:
        assert isinstance(handler, RotatingFileHandler)
        assert handler.baseFilename == str(tmp_path / "from-env.log")
    finally:
        configure_logging(target="console")


def test_unknown_log_target_fails_with_a_clear_message():
    with pytest.raises(ValueError, match="LOG_TARGET must be 'console' or 'file', not 'printer'"):
        configure_logging(target="printer")


def test_relative_log_file_is_inside_the_backend_folder():
    assert resolve_log_file("logs/app.log") == BACKEND_DIR / "logs" / "app.log"
    assert resolve_log_file(None) == BACKEND_DIR / "logs" / "backend.log"


def test_reconfiguring_does_not_print_every_line_twice(log_file):
    configure_logging(target="file", log_file=log_file)  # a second call, as on a reload
    ours = [h for h in logging.getLogger("backend").handlers if h.get_name()]
    assert len(ours) == 1


def test_log_level_warning_hides_routine_request_lines(client, log_file):
    configure_logging(target="file", log_file=log_file, level="WARNING")
    client.get("/orders/1042")  # a success: INFO, so not written
    client.get("/orders/9999")  # a 404: WARNING, so written
    text = log_file.read_text(encoding="utf-8")
    assert "path=/orders/1042" not in text
    assert "request_failed status=404 code=not_found" in text


# --- What each line says ---


def test_each_request_gets_one_line_and_an_id_header(client, log_file):
    response = client.get("/orders/1042")
    request_id = response.headers["X-Request-ID"]
    matching = [line for line in lines(log_file) if f"[req={request_id}]" in line]
    assert len(matching) == 1
    assert " INFO " in matching[0]
    assert "request method=GET path=/orders/1042 status=200 duration_ms=" in matching[0]


def test_refund_lines_share_the_request_id(client, log_file):
    response = post_refund(client)
    request_id = response.headers["X-Request-ID"]
    mine = [line for line in lines(log_file) if f"[req={request_id}]" in line]
    assert any(
        "refund_created order_id=1042 refund_id=2 amount_paise=349900 reason=damaged"
        " status=completed" in line
        for line in mine
    )
    assert any("request method=POST path=/orders/1042/refunds status=201" in line for line in mine)


def test_retry_is_logged_as_a_replay(client, log_file):
    post_refund(client)
    post_refund(client)
    assert any("refund_replayed order_id=1042 refund_id=2" in line for line in lines(log_file))


def test_policy_refusal_is_a_warning_with_its_code_and_message(client, log_file):
    post_refund(client, order_id=1043, body={"reason": "damaged", "amount_paise": 219_900})
    refused = [line for line in lines(log_file) if "request_failed" in line]
    assert len(refused) == 1
    assert " WARNING " in refused[0]
    assert 'status=422 code=policy_violation message="Order 1043 was delivered 45 days ago.' in (
        refused[0]
    )


def test_cancel_is_logged_only_when_it_changes_the_order(client, log_file):
    client.post("/orders/1048/cancel")
    client.post("/orders/1048/cancel")  # already cancelled: nothing changes
    assert sum("order_cancelled order_id=1048" in line for line in lines(log_file)) == 1


def test_crash_is_logged_as_an_error_with_its_stack_trace(client, db_path, log_file):
    conn = connect(db_path)
    conn.execute("DROP TABLE order_items")
    conn.close()
    crashing = TestClient(app, raise_server_exceptions=False, headers={"X-API-Key": TEST_API_KEY})
    crashing.get("/orders/1042")
    text = log_file.read_text(encoding="utf-8")
    assert "request method=GET path=/orders/1042 status=500" in text
    assert "Traceback" in text  # the operator sees what went wrong; the client did not


# --- What must never be logged ---


def test_api_keys_are_never_logged(client, log_file):
    client.get("/orders/1042")  # the correct key
    client.get("/orders/1042", headers={"X-API-Key": "a-wrong-but-secret-guess"})
    text = log_file.read_text(encoding="utf-8")
    assert TEST_API_KEY not in text
    assert "a-wrong-but-secret-guess" not in text
    assert "request_failed status=401 code=unauthorized" in text  # the failure is still recorded
