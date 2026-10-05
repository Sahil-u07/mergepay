"""Minimal PayPal client for the Payouts API (sandbox by default).

Request and response shapes follow PayPal's OpenAPI spec:
https://github.com/paypal/paypal-rest-api-specifications/blob/main/openapi/payments_payouts_batch_v1.json
"""
import json
import os
import time

import httpx

SANDBOX = "https://api-m.sandbox.paypal.com"


class PayPalError(Exception):
    """status 0 means the outcome is unknown (no answer, or an answer we couldn't read):
    the payout may or may not exist, so nobody should retry blindly."""

    def __init__(self, status: int, body: str):
        super().__init__(f"PayPal returned {status}: {body[:500]}")
        self.status = status
        self.existing_batch = _existing_batch(body)


def _existing_batch(body: str) -> str | None:
    """PayPal answers a reused sender_batch_id with a link to the batch it already has."""
    try:
        details = json.loads(body).get("details", [])
    except (ValueError, AttributeError):
        return None
    for d in details:
        if d.get("field") == "SENDER_BATCH_ID":
            for link in d.get("link", []):
                return link["href"].rstrip("/").rsplit("/", 1)[-1]
    return None


class PayPal:
    def __init__(self, client_id: str, secret: str, base_url: str = SANDBOX, http: httpx.Client | None = None):
        self.auth = (client_id, secret)
        self.http = http or httpx.Client(base_url=base_url, timeout=30)
        self._token, self._expires = None, 0.0

    @classmethod
    def from_env(cls):
        return cls(os.environ["PAYPAL_CLIENT_ID"], os.environ["PAYPAL_CLIENT_SECRET"],
                   os.environ.get("PAYPAL_BASE_URL", SANDBOX))

    def _headers(self) -> dict:
        # OAuth2 client credentials. Reuse the token until 60s before it expires.
        if time.time() > self._expires - 60:
            r = self.http.post("/v1/oauth2/token", auth=self.auth, data={"grant_type": "client_credentials"})
            r.raise_for_status()
            body = r.json()
            self._token, self._expires = body["access_token"], time.time() + body["expires_in"]
        return {"Authorization": f"Bearer {self._token}"}

    def _call(self, method: str, path: str, **kw) -> dict:
        try:
            r = self.http.request(method, path, headers=self._headers(), **kw)
        except httpx.HTTPError as e:  # network down, timeout, or bad credentials on the token call
            raise PayPalError(0, str(e))
        if r.is_error:
            raise PayPalError(r.status_code, r.text)
        try:
            return r.json() if r.content else {}
        except ValueError:  # a 2xx we can't read: the call may have worked
            raise PayPalError(0, f"unreadable {r.status_code} response: {r.text}")

    def pay(self, bounty_id: str, email: str, amount: str, currency: str, note: str) -> dict:
        """Send one payout. Returns PayPal's batch_header (payout_batch_id, batch_status).

        bounty_id is used as sender_batch_id. PayPal refuses a sender_batch_id it has
        seen in the last 30 days (PayPalError.existing_batch names the old batch), so a
        retry within that window can't pay twice.
        """
        body = {
            "sender_batch_header": {
                "sender_batch_id": bounty_id,
                "email_subject": "You received a bounty payment",
            },
            "items": [{
                "recipient_type": "EMAIL",
                "receiver": email,
                "amount": {"value": amount, "currency": currency},
                "note": note[:1000],
                "sender_item_id": bounty_id,
            }],
        }
        return self._call("POST", "/v1/payments/payouts", json=body)["batch_header"]

    def payout_status(self, payout_batch_id: str) -> str:
        """Status of the batch's single item: SUCCESS, PENDING, UNCLAIMED, RETURNED, FAILED, ..."""
        body = self._call("GET", f"/v1/payments/payouts/{payout_batch_id}")
        items = body.get("items") or []
        return items[0]["transaction_status"] if items else body["batch_header"]["batch_status"]
