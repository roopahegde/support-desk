# Backend Test Cases

Plain-English test cases for the Support Desk backend API. Each case can be run by hand (in
Swagger UI or with curl), and each one is also automated: the **Automated test** column names
the pytest function that checks it, in the file named in that section's heading.

## How to read this document

- **ID:** a stable reference for bug reports, for example "TC-REF-07 fails".
- **Expected result:** what must happen. `[x]` in a case's ID means it is checked once for
  each listed variant (for example each invalid value), and counts as one case here.
- **Money** is in paise (1 INR = 100 paise), so `349900` is INR 3,499.
- **Error bodies** always look like `{"error": {"code": "...", "message": "..."}}`. Where a case
  gives a code, the body must contain exactly that code.

## Before you start (manual testing)

1. Follow the setup in [../README.md](../README.md) and start the server.
2. **Reset the data before each session:** `python -m backend.app.seed`. Dates are calculated
   relative to when you seed, so stale data gives wrong refund-window results.
3. In Swagger UI (http://127.0.0.1:8000/docs), click **Authorize** and enter the API key,
   unless a case says to send no key.
4. Refund requests need an `Idempotency-Key` header. Use a new value per case (for example
   `tc-ref-01`) unless the case says to reuse one.
5. Cases that change data (refunds, cancellations) affect later cases. Re-seed between them
   when in doubt.

Some cases need data or failures that can't be produced by hand (for example a database crash,
or two requests racing each other). These are marked **Automated only**.

### Test data (from the seed)

| Order | Status | Delivered | Total | Used for |
|---|---|---|---|---|
| 1042 | delivered | 5 days ago | 349900 | Happy-path refund |
| 1043 | delivered | 45 days ago | 219900 | Outside the 30-day window |
| 1044 | delivered | 10 days ago | 5499900 | Above INR 10,000: needs approval |
| 1045 | refunded | 14 days ago | 129900 | Already refunded (key `seed-refund-1045`) |
| 1046 | delivered | 3 days ago | 189900 | Contains the out-of-stock kettle |
| 1047 | shipped | – | 249900 | Not delivered yet |
| 1048 | placed | – | 259800 | Not shipped: can be cancelled |
| 1049 | delivered | 20 days ago | 249900 | `damaged` allowed, `changed_mind` not |

Customers 1, 2 and 3 exist. Product `SPK-200` has 25 in stock, and `KTL-500` has 0.

---

## 1. Seed data — `test_seed.py`

| ID | Scenario | Steps | Expected result | Automated test |
|---|---|---|---|---|
| TC-SEED-01 | Happy-path order is set up | Look up order 1042 | Status `delivered`, total `349900`, delivered exactly 5 days ago | `test_order_1042_is_the_happy_path` |
| TC-SEED-02 | An order outside the refund window exists | Look up order 1043 | Delivered exactly 45 days ago | `test_order_1043_is_outside_the_30_day_window` |
| TC-SEED-03 | An order above the approval threshold exists | Look up order 1044 | Total is more than `1000000` (INR 10,000) | `test_order_1044_is_above_the_approval_threshold` |
| TC-SEED-04 | An already-refunded order exists | Look up order 1045 and its refund | Status `refunded`; its refund has idempotency key `seed-refund-1045` | `test_order_1045_is_already_refunded` |
| TC-SEED-05 | An out-of-stock product exists | Look up product `KTL-500` | Stock is `0` | `test_kettle_is_out_of_stock` |
| TC-SEED-06 | The database itself allows one refund per order | Insert a second refund for order 1045 directly in the database | The database rejects it (integrity error) | `test_database_rejects_a_second_refund_for_the_same_order` |

## 2. Business rules — `test_rules.py`

These cases test the rules directly, without the API, so they can check exact boundaries.
"Now" is fixed for each case.

### Refund rules

| ID | Scenario | Input | Expected result | Automated test |
|---|---|---|---|---|
| TC-RULE-01 | Eligible refund | Delivered 5 days ago, `damaged`, full amount | Allowed, status `completed` | `test_eligible_refund_is_completed` |
| TC-RULE-02[x] | Order not delivered | Status `placed`, `shipped`, `cancelled` or `refunded` | Refused: "...has not been delivered (its status is '&lt;status&gt;')" | `test_order_that_is_not_delivered_cannot_be_refunded` |
| TC-RULE-03 | Last day of the window | Delivered exactly 30 days ago | Allowed | `test_refund_on_the_last_day_of_the_window_is_allowed` |
| TC-RULE-04 | Just after the window | Delivered 30 days and 1 second ago | Refused: "...only allowed within 30 days of delivery" | `test_refund_just_after_the_window_is_rejected` |
| TC-RULE-05 | Message states the order's age | Delivered 45 days ago | Refused, and the message says "was delivered 45 days ago" | `test_order_delivered_45_days_ago_is_rejected_with_its_age` |
| TC-RULE-06 | `changed_mind` on day 7 | Delivered exactly 7 days ago, reason `changed_mind` | Allowed | `test_changed_mind_is_allowed_within_7_days` |
| TC-RULE-07 | `changed_mind` after day 7 | Delivered 7 days and 1 second ago, reason `changed_mind` | Refused: "'changed_mind' is only accepted within 7 days" | `test_changed_mind_just_after_7_days_is_rejected` |
| TC-RULE-08[x] | Other reasons keep the full window | Delivered 20 days ago, reason `damaged`, `wrong_item` or `not_as_described` | Allowed | `test_other_reasons_keep_the_full_30_day_window` |
| TC-RULE-09 | Refund of the full total | Amount equals the order total | Allowed | `test_refund_of_the_full_order_total_is_allowed` |
| TC-RULE-10 | Refund above the total | Amount is 1 paisa more than the total | Refused: "...more than the order total" | `test_refund_above_the_order_total_is_rejected` |
| TC-RULE-11 | Exactly INR 10,000 | Amount `1000000` | Allowed, status `completed` (no approval needed) | `test_refund_of_exactly_10000_rupees_does_not_need_approval` |
| TC-RULE-12 | Just above INR 10,000 | Amount `1000001` | Allowed, status `pending_approval` | `test_refund_above_10000_rupees_needs_approval` |
| TC-RULE-13 | Ineligible large refund is refused, not escalated | Delivered 45 days ago, amount above INR 10,000 | Refused (it is not sent for approval) | `test_window_is_checked_before_the_approval_threshold` |

### Cancellation rules

| ID | Scenario | Input | Expected result | Automated test |
|---|---|---|---|---|
| TC-RULE-14[x] | Orders that can be cancelled | Status `placed` or `cancelled` | Allowed (cancelling twice is harmless) | `test_order_not_yet_shipped_or_already_cancelled_can_be_cancelled` |
| TC-RULE-15[x] | Orders that can't be cancelled | Status `shipped`, `delivered` or `refunded` | Refused, with a message saying why and what to do instead | `test_order_that_has_shipped_cannot_be_cancelled` |

## 3. Reading orders, stock and policy — `test_api.py`

| ID | Scenario | Steps | Expected result | Automated test |
|---|---|---|---|---|
| TC-ORD-01 | Get an order | `GET /orders/1042` | `200`; the order with its dates, total `349900` and one item `SPK-200` | `test_get_order_returns_the_order_with_its_items` |
| TC-ORD-02 | Order that has not shipped | `GET /orders/1048` | `200`; `shipped_at` and `delivered_at` are `null`; item quantity is 2 | `test_get_order_not_yet_shipped_has_null_dates` |
| TC-ORD-03 | Unknown order | `GET /orders/9999` | `404`, code `not_found`, "Order 9999 not found" | `test_get_order_unknown_id_returns_404` |
| TC-ORD-04 | Order ID is not a number | `GET /orders/abc` | `422` | `test_get_order_non_integer_id_returns_422` |
| TC-CUS-01 | A customer's orders | `GET /customers/1/orders` | `200`; orders 1048, 1042, 1043 in that order (newest first), each with its own items | `test_customer_orders_are_newest_first_with_their_own_items` |
| TC-CUS-02 | Order with two items | Add a second item to order 1042, then `GET /customers/1/orders` | Order 1042 appears **once**, with both items. **Automated only** | `test_customer_order_with_two_items_is_listed_once` |
| TC-CUS-03 | Order with no items | Add an order with no items for customer 1, then `GET /customers/1/orders` | That order appears, with `items: []`. **Automated only** | `test_customer_order_without_items_is_kept` |
| TC-CUS-04 | Customer with no orders | Add a customer with no orders, then list their orders | `200` with `[]` (not a 404). **Automated only** | `test_customer_without_orders_gets_an_empty_list` |
| TC-CUS-05 | Unknown customer | `GET /customers/999/orders` | `404`, code `not_found`, "Customer 999 not found" | `test_unknown_customer_returns_404` |
| TC-CUS-06 | Customer ID is not a number | `GET /customers/abc/orders` | `422` | `test_customer_non_integer_id_returns_422` |
| TC-STK-01 | Product in stock | `GET /products/SPK-200/stock` | `200`; `{"sku": "SPK-200", "name": "Bluetooth Speaker", "stock": 25}` | `test_product_stock_returns_the_current_level` |
| TC-STK-02 | Product out of stock | `GET /products/KTL-500/stock` | `200` with `stock: 0` (not an error) | `test_out_of_stock_product_is_not_an_error` |
| TC-STK-03 | Unknown product | `GET /products/NOPE-1/stock` | `404`, code `not_found`, "Product NOPE-1 not found" | `test_unknown_product_returns_404` |
| TC-POL-01 | Refund policy | `GET /policies/refund` | `200`; window 30 days, `changed_mind` window 7 days, the four reasons, threshold `1000000`, max 1 refund per order | `test_refund_policy_publishes_the_business_rules` |

## 4. Refunds — `test_api.py`

Every request below sends a valid API key and an `Idempotency-Key`, unless the case says otherwise.

### Creating refunds

| ID | Scenario | Steps | Expected result | Automated test |
|---|---|---|---|---|
| TC-REF-01 | Successful refund | `POST /orders/1042/refunds` with `{"reason": "damaged", "amount_paise": 349900}` | `201`; refund for order 1042, status `completed`; the idempotency key is **not** in the response | `test_create_refund_returns_201_with_the_refund` |
| TC-REF-02 | Order is marked refunded | Do TC-REF-01, then `GET /orders/1042` | Order status is `refunded` | `test_create_refund_marks_the_order_as_refunded` |
| TC-REF-03 | Second refund with a new key | Do TC-REF-01, then repeat it with a **different** key | `409`, code `already_refunded`; still only one refund | `test_second_refund_with_a_new_key_returns_409` |
| TC-REF-04 | Order refunded in the seed | `POST /orders/1045/refunds` with a new key | `409` | `test_refund_of_seeded_refunded_order_returns_409` |
| TC-REF-05 | Unknown order | `POST /orders/9999/refunds` | `404`, code `not_found` | `test_refund_of_unknown_order_returns_404` |
| TC-REF-06[x] | Invalid request body | Reason `bored`; amount `0`, `-100` or `"lots"`; amount missing; reason missing | `422` each time; order 1042 is still `delivered` | `test_invalid_refund_body_returns_422_and_changes_nothing` |
| TC-REF-07 | Failure halfway through is undone | Make the order's status update fail after the refund is saved | `500`, code `internal_error`; **no** refund is left behind. **Automated only** | `test_failed_status_update_rolls_back_the_refund` |
| TC-REF-08 | Refund time comes from the server clock | Create a refund | `created_at` is the server's current time | `test_refund_uses_the_injected_clock_for_created_at` |

### Refund policy through the API

| ID | Scenario | Steps | Expected result | Automated test |
|---|---|---|---|---|
| TC-REF-09[x] | Refunds the policy forbids | Order 1043 (45 days ago); 1047 (shipped); 1048 (placed); 1049 with `changed_mind`; 1042 with amount `349901` | `422`, code `policy_violation`, with the exact reason; no refund is saved and the order's status is unchanged | `test_refund_the_policy_forbids_returns_422_and_changes_nothing` |
| TC-REF-10 | `damaged` after 7 days | `POST /orders/1049/refunds`, reason `damaged`, amount `249900` | `201`, status `completed` | `test_damaged_refund_is_still_allowed_after_7_days` |
| TC-REF-11 | Large refund needs approval | `POST /orders/1044/refunds`, amount `5499900` | `201`, status `pending_approval`; order 1044 is still `delivered` | `test_large_refund_is_saved_for_approval_without_refunding_the_order` |
| TC-REF-12 | New request for a pending refund | Do TC-REF-11, then repeat it with a different key | `409`, code `refund_pending_approval`, "...waiting for human approval" | `test_second_request_for_a_pending_refund_says_it_awaits_approval` |
| TC-REF-13 | Retry of a pending refund | Do TC-REF-11, then repeat it with the **same** key | `201`; the same pending refund is returned | `test_retry_of_a_pending_refund_returns_it_unchanged` |
| TC-REF-14 | Retry after the window has closed | Create a refund, then retry it with the same key 60 days later | `201`; the original refund is returned (the rules apply only when a refund is created). **Automated only** | `test_retry_after_the_window_still_returns_the_original_refund` |

### Idempotency (safe retries)

| ID | Scenario | Steps | Expected result | Automated test |
|---|---|---|---|---|
| TC-IDEM-01 | Retry with the same key | Send TC-REF-01 twice with the same key | Both `201` with identical bodies; only **one** refund exists | `test_retry_with_the_same_key_returns_the_original_refund` |
| TC-IDEM-02 | Replay the seeded refund | `POST /orders/1045/refunds`, key `seed-refund-1045`, `{"reason": "damaged", "amount_paise": 129900}` | `201`; the seeded refund for order 1045 is returned | `test_seeded_refund_can_be_replayed_with_its_key` |
| TC-IDEM-03[x] | Same key, different request | Do TC-REF-01, then reuse its key with a different amount, a different reason, or for order 1046 | `409`, code `idempotency_key_reused`; order 1046 is not refunded | `test_reusing_a_key_for_a_different_request_returns_409` |
| TC-IDEM-04 | Key header missing | Send TC-REF-01 without `Idempotency-Key` | `422`; the error names the field `header.idempotency-key`; no refund is created | `test_missing_idempotency_key_returns_422` |
| TC-IDEM-05[x] | Key empty or too long | Key is empty, or 256 characters long | `422` | `test_empty_or_oversized_idempotency_key_returns_422` |
| TC-IDEM-06 | Racing retry (passed the key check) | A retry arrives while its original is still being saved | `201`; the original refund is returned; one refund exists. **Automated only** | `test_retry_racing_past_the_key_lookup_returns_the_original_refund` |
| TC-IDEM-07 | Racing request with a new key (passed the key check) | A different request for the same order arrives while the first is being saved | `409`, code `already_refunded`; one refund exists. **Automated only** | `test_new_key_racing_past_the_key_lookup_returns_409` |
| TC-IDEM-08 | Racing retry (reached the database write) | As TC-IDEM-06, but both requests pass every check before writing | `201`; the original refund is returned; one refund exists. **Automated only** | `test_retry_racing_to_the_insert_returns_the_original_refund` |
| TC-IDEM-09 | Racing new key (reached the database write) | As TC-IDEM-07, but both requests pass every check before writing | `409`, code `refund_pending_approval`; one refund exists. **Automated only** | `test_new_key_racing_to_the_insert_returns_409` |

## 5. Cancelling orders — `test_api.py`

| ID | Scenario | Steps | Expected result | Automated test |
|---|---|---|---|---|
| TC-CAN-01 | Cancel an order that has not shipped | `POST /orders/1048/cancel` | `200`; status `cancelled`, items included; `GET /orders/1048` also shows `cancelled` | `test_cancel_an_order_that_has_not_shipped` |
| TC-CAN-02 | Cancel twice | Repeat TC-CAN-01 | `200` both times, with identical bodies | `test_cancelling_twice_is_harmless` |
| TC-CAN-03[x] | Orders that can't be cancelled | `POST /orders/1047/cancel` (shipped), `/1042/cancel` (delivered), `/1045/cancel` (refunded) | `422`, code `policy_violation`, saying why; the status is unchanged | `test_order_past_placed_cannot_be_cancelled` |
| TC-CAN-04 | Unknown order | `POST /orders/9999/cancel` | `404`, code `not_found` | `test_cancel_unknown_order_returns_404` |
| TC-CAN-05 | No API key | `POST /orders/1048/cancel` without a key | `401`; order 1048 is still `placed` | `test_cancel_requires_an_api_key` |
| TC-CAN-06 | A cancelled order can't be refunded | Do TC-CAN-01, then refund order 1048 | `422`, code `policy_violation`, mentioning status `cancelled` | `test_cancelled_order_cannot_be_refunded` |
| TC-CAN-07 | Order ships during the cancel | The order ships between the cancel's read and its write | `422` "has already shipped"; the order stays `shipped`. **Automated only** | `test_order_that_ships_mid_request_is_not_cancelled` |

## 6. Security (API key) — `test_api.py`

| ID | Scenario | Steps | Expected result | Automated test |
|---|---|---|---|---|
| TC-SEC-01 | Correct key | `GET /orders/1042` with the right key | `200` | `test_correct_api_key_is_accepted` |
| TC-SEC-02 | No key | `GET /orders/1042` without `X-API-Key` | `401`, code `unauthorized`, "Invalid API key" | `test_missing_api_key_returns_401` |
| TC-SEC-03 | Wrong key | `GET /orders/1042` with `X-API-Key: wrong` | `401`, with the **same** body as TC-SEC-02, so a caller can't tell which mistake they made | `test_wrong_api_key_gets_the_same_401_as_a_missing_one` |
| TC-SEC-04[x] | Every read endpoint is protected | Call each `GET` endpoint without a key | `401` each time | `test_every_read_endpoint_requires_a_key` |
| TC-SEC-05 | Refund without a key | A valid refund request without a key | `401`; order 1042 is still `delivered` | `test_refund_without_a_key_is_rejected_and_changes_nothing` |
| TC-SEC-06 | No details leak without a key | `GET /orders/abc` without a key | `401` (not a `422` with validation details) | `test_invalid_id_without_a_key_gets_401_not_validation_details` |
| TC-SEC-07 | Server has no key configured | Start the server without `BACKEND_API_KEY`, then call any endpoint | `500`, code `server_misconfigured`, saying it is not the caller's fault; no key appears in the response | `test_server_with_no_key_configured_rejects_requests` |
| TC-SEC-08 | Operator is told how to fix it | As TC-SEC-07, then read the server log | The log says "BACKEND_API_KEY is not set" | `test_missing_server_key_is_logged_for_the_operator` |
| TC-SEC-09 | Server key set but empty | Set `BACKEND_API_KEY=` (empty), then call with no key and with an empty key | `500` both times (an empty key is never accepted) | `test_server_with_an_empty_key_is_misconfigured` |
| TC-SEC-10 | Key with non-English characters | Send `X-API-Key: é` | `401` (not a server error). **Automated only** | `test_non_ascii_api_key_gets_401_not_a_server_error` |
| TC-SEC-11 | `.env` is found wherever the server starts | Check where the server looks for its `.env` | Always `backend/.env`, whatever the current folder is | `test_env_file_is_found_from_the_code_not_the_current_directory` |
| TC-SEC-12 | Docs stay public | Open `/docs` and `/openapi.json` without a key | `200` | `test_docs_stay_public` |
| TC-SEC-13 | Swagger offers the key field | Open `/openapi.json` | It declares an API key in the `X-API-Key` header (the **Authorize** button) | `test_swagger_advertises_the_api_key_header` |

## 7. Error format — `test_api.py`

| ID | Scenario | Steps | Expected result | Automated test |
|---|---|---|---|---|
| TC-ERR-01 | Validation errors list each field | Refund with `{"reason": "bored", "amount_paise": 0}` | `422`, code `validation_error`; `fields` lists `body.reason` and `body.amount_paise`; the message summarises both | `test_validation_error_lists_each_field_with_a_readable_summary` |
| TC-ERR-02 | Bad URL parameter | `GET /orders/abc` | Code `validation_error`, field `path.order_id` | `test_invalid_path_parameter_uses_the_standard_format` |
| TC-ERR-03 | Unknown URL | `GET /no-such-endpoint` | `404`, code `not_found`, "There is no endpoint at /no-such-endpoint." | `test_unknown_url_uses_the_standard_format` |
| TC-ERR-04 | Wrong HTTP method | `DELETE /orders/1042` | `405`, code `method_not_allowed`, "DELETE is not allowed on /orders/1042." | `test_wrong_http_method_uses_the_standard_format` |
| TC-ERR-05 | Crash details stay private | Make an endpoint crash (for example, a missing database table) | `500`, code `internal_error`; no table names or stack trace in the response. **Automated only** | `test_unexpected_error_hides_internal_details` |
| TC-ERR-06 | Swagger documents the error format | Open `/openapi.json` | The refund endpoint's `401`, `422` and `500` responses use `ErrorResponse`; FastAPI's default validation schema is gone | `test_swagger_documents_the_standard_error_body` |

## 8. Logging — `test_logging.py`

For manual checks, set `LOG_TARGET=file` in `backend/.env`, restart the server, and read
`backend/logs/backend.log`.

| ID | Scenario | Steps | Expected result | Automated test |
|---|---|---|---|---|
| TC-LOG-01 | Console is the default | Start with no `LOG_TARGET` set | Log lines go to the console | `test_console_is_the_default` |
| TC-LOG-02 | Switch to a file | Set `LOG_TARGET=file` and `LOG_FILE`, then start | Log lines go to that file, which rotates when it grows | `test_log_target_file_writes_to_a_rotating_file` |
| TC-LOG-03 | Invalid target | Set `LOG_TARGET=printer`, then start | Startup fails: "LOG_TARGET must be 'console' or 'file', not 'printer'" | `test_unknown_log_target_fails_with_a_clear_message` |
| TC-LOG-04 | Relative file path | Set `LOG_FILE=logs/app.log` | The file is `backend/logs/app.log`, whatever the current folder | `test_relative_log_file_is_inside_the_backend_folder` |
| TC-LOG-05 | No duplicate lines | Reconfigure logging (as happens on a reload) | Each line is written once | `test_reconfiguring_does_not_print_every_line_twice` |
| TC-LOG-06 | Log level filters lines | Set `LOG_LEVEL=WARNING`; get order 1042, then order 9999 | No line for the successful request; the `404` is logged | `test_log_level_warning_hides_routine_request_lines` |
| TC-LOG-07 | One line per request, with an ID | `GET /orders/1042` and note the `X-Request-ID` response header | Exactly one `request` line with that ID, showing method, path, status `200` and duration | `test_each_request_gets_one_line_and_an_id_header` |
| TC-LOG-08 | A refund's lines share its ID | Do TC-REF-01 | A `refund_created` line (order, refund ID, amount, reason, status) and the `request` line carry the same request ID | `test_refund_lines_share_the_request_id` |
| TC-LOG-09 | Retries are visible | Do TC-IDEM-01 | A `refund_replayed` line for order 1042 | `test_retry_is_logged_as_a_replay` |
| TC-LOG-10 | Refusals are warnings | Refund order 1043 | Exactly one `WARNING` `request_failed` line with `status=422 code=policy_violation` and the message | `test_policy_refusal_is_a_warning_with_its_code_and_message` |
| TC-LOG-11 | Cancellation logged once | Cancel order 1048 twice | Exactly one `order_cancelled` line | `test_cancel_is_logged_only_when_it_changes_the_order` |
| TC-LOG-12 | Crashes are logged in full | Make an endpoint crash | A `status=500` request line and the full stack trace are in the log (but not in the response). **Automated only** | `test_crash_is_logged_as_an_error_with_its_stack_trace` |
| TC-LOG-13 | API keys are never logged | Call with the correct key, then with a wrong one | Neither key appears anywhere in the log; the `401` failure is still recorded | `test_api_keys_are_never_logged` |
