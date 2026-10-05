"""PayPal client tests against a fake server, so no network or keys are needed."""
import json

import httpx
import pytest

from paypal import PayPal, PayPalError


def make(handler):
    calls = []

    def record(request):
        calls.append(request)
        if request.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        return handler(request)

    http = httpx.Client(base_url="https://api-m.sandbox.paypal.com", transport=httpx.MockTransport(record))
    return PayPal("id", "secret", http=http), calls


def created(request):
    return httpx.Response(201, json={"batch_header": {"payout_batch_id": "B1", "batch_status": "PENDING"}})


def test_pay_sends_one_email_payout_keyed_by_bounty():
    pp, calls = make(created)
    header = pp.pay("bounty-7", "dev@example.com", "40.00", "USD", "Thanks for fixing #7")
    assert header == {"payout_batch_id": "B1", "batch_status": "PENDING"}
    body = json.loads(calls[-1].content)
    assert calls[-1].url.path == "/v1/payments/payouts"
    assert body["sender_batch_header"]["sender_batch_id"] == "bounty-7"
    assert body["items"] == [{"recipient_type": "EMAIL", "receiver": "dev@example.com",
                              "amount": {"value": "40.00", "currency": "USD"},
                              "note": "Thanks for fixing #7", "sender_item_id": "bounty-7"}]


def test_token_is_reused():
    pp, calls = make(created)
    pp.pay("a", "x@example.com", "1.00", "USD", "")
    pp.pay("b", "x@example.com", "1.00", "USD", "")
    assert [c.url.path for c in calls].count("/v1/oauth2/token") == 1
    assert calls[-1].headers["Authorization"] == "Bearer tok"


def test_duplicate_payout_is_an_error_not_a_second_payment():
    # Example error body; the exact wording PayPal uses may differ.
    pp, _ = make(lambda r: httpx.Response(400, json={"name": "USER_BUSINESS_ERROR",
                                                       "message": "Batch with given sender_batch_id already exists"}))
    with pytest.raises(PayPalError, match="400.*already exists"):
        pp.pay("bounty-7", "dev@example.com", "40.00", "USD", "")


def test_network_failure_becomes_paypal_error():
    def boom(request):
        raise httpx.ConnectError("no route")
    pp, _ = make(boom)
    with pytest.raises(PayPalError, match="no route"):
        pp.payout_status("B1")


# The body PayPal really sent when a bounty's batch id was reused (sandbox, Oct 2026).
DUPLICATE = {"name": "USER_BUSINESS_ERROR", "message": "User business error.", "debug_id": "f7796227d93d5",
             "information_link": "https://developer.paypal.com/docs/api/payments.payouts-batch/#errors",
             "details": [{"field": "SENDER_BATCH_ID", "location": "body",
                          "issue": "Batch with given sender_batch_id already exists",
                          "link": [{"href": "https://api.sandbox.paypal.com/v1/payments/payouts/LXVBBMVWA2GK6",
                                    "rel": "self", "method": "GET", "encType": "application/json"}]}],
             "links": []}


def test_duplicate_payout_names_the_existing_batch():
    pp, _ = make(lambda r: httpx.Response(400, json=DUPLICATE))
    with pytest.raises(PayPalError) as e:
        pp.pay("mergepay-abc", "dev@example.com", "40.00", "USD", "")
    assert e.value.existing_batch == "LXVBBMVWA2GK6"


def test_other_errors_have_no_existing_batch():
    pp, _ = make(lambda r: httpx.Response(422, json={"name": "INSUFFICIENT_FUNDS", "message": "Sender has insufficient funds."}))
    with pytest.raises(PayPalError) as e:
        pp.pay("mergepay-abc", "dev@example.com", "40.00", "USD", "")
    assert e.value.existing_batch is None and e.value.status == 422


def test_unreadable_success_means_unknown_outcome():
    # A 2xx we can't read may still have created the payout, so it's reported as status 0 (unknown).
    pp, _ = make(lambda r: httpx.Response(201, content=b"<html>proxy page</html>"))
    with pytest.raises(PayPalError) as e:
        pp.pay("mergepay-abc", "dev@example.com", "40.00", "USD", "")
    assert e.value.status == 0


def test_payout_status_is_the_item_status():
    # The batch can be SUCCESS while the single item is still UNCLAIMED; the item is what matters.
    pp, calls = make(lambda r: httpx.Response(200, json={
        "batch_header": {"payout_batch_id": "B1", "batch_status": "SUCCESS"},
        "items": [{"payout_item_id": "I1", "transaction_status": "UNCLAIMED", "payout_batch_id": "B1"}]}))
    assert pp.payout_status("B1") == "UNCLAIMED"
    assert calls[-1].url.path == "/v1/payments/payouts/B1"
