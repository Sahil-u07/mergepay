"""End-to-end tests of the webhook flow with fake GitHub, AI and PayPal."""
import hashlib
import hmac
import json
import os
from decimal import Decimal

os.environ.update(GITHUB_TOKEN="t", GITHUB_WEBHOOK_SECRET="whsec", ADMIN_PASSWORD="pw", DB_PATH=":memory:",
                  PAYPAL_CLIENT_ID="x", PAYPAL_CLIENT_SECRET="y", GEMINI_API_KEY="test")

import httpx
import pytest
from fastapi.testclient import TestClient

import app
import db
from judge import Verdict
from paypal import PayPalError


class FakeGitHub:
    def __init__(self): self.title, self.missing = "Crash on empty name", False
    def issue(self, repo, n):
        if self.missing:
            raise httpx.HTTPStatusError("404 Not Found", request=httpx.Request("GET", "https://api.github.com"),
                                        response=httpx.Response(404))
        return {"title": self.title, "body": "Steps..."}
    def pr_diff(self, repo, n): return "+ if not name: return"


class FakePayPal:
    def __init__(self, error=None, status="SUCCESS"): self.paid, self.error, self.status = [], error, status
    def pay(self, bounty_id, email, amount, currency, note):
        if self.error:
            raise self.error
        self.paid.append((bounty_id, email, amount))
        return {"payout_batch_id": "B1", "batch_status": "PENDING"}
    def payout_status(self, batch_id): return self.status


def verdict(**kw):
    return Verdict(**{"solves_issue": True, "confidence": "high", "summary": "Adds the null check.",
                      "concerns": [], "manipulation_attempt": False, **kw})


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(app, "conn", db.connect(":memory:"))
    monkeypatch.setattr(app, "github", FakeGitHub())
    pp = FakePayPal()
    monkeypatch.setattr(app, "paypal", pp)
    monkeypatch.setattr(app.judge, "judge", lambda *a: verdict())
    db.set_contributor(app.conn, "dev", "dev@example.com")
    # The dashboard sends X-MergePay on every write; see test_writes_need_the_dashboard_header.
    return TestClient(app.app, headers={"X-MergePay": "1"}), pp


def send(client, payload, event="pull_request", secret="whsec"):
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post("/webhook", content=body, headers={
        "X-Hub-Signature-256": sig, "X-GitHub-Event": event, "Content-Type": "application/json"})


def merged(body="Fixes #7", merged=True, base="main"):
    return {"action": "closed", "repository": {"full_name": "o/r", "default_branch": "main"},
            "pull_request": {"number": 12, "merged": merged, "title": "Fix crash", "body": body,
                             "user": {"login": "dev"}, "base": {"ref": base}}}


def test_pr_merged_into_other_branch_is_ignored(env):
    # GitHub only closes the issue when the PR lands on the default branch.
    client, pp = env
    bid = db.create_bounty(app.conn, "o/r", 7, Decimal("40"))
    assert "ignored" in send(client, merged(base="experiment")).json()
    assert pp.paid == []
    assert db.get_bounty(app.conn, bid)["status"] == "open"


def test_small_clean_bounty_is_paid(env):
    client, pp = env
    bid = db.create_bounty(app.conn, "o/r", 7, Decimal("40"))
    assert send(client, merged()).json() == {"reviewing": [7]}
    assert pp.paid == [(f"mergepay-{db.get_bounty(app.conn, bid)['ref']}", "dev@example.com", "40")]
    assert db.get_bounty(app.conn, bid)["status"] == "paid"


def test_batch_id_is_unique_across_databases():
    # Render's free plan wipes the database on redeploy, so bounty ids restart at 1.
    # PayPal remembers old batch ids, so they must not be built from the id alone.
    a, b = db.connect(":memory:"), db.connect(":memory:")
    ida, idb = db.create_bounty(a, "o/r", 7, Decimal("5")), db.create_bounty(b, "o/r", 7, Decimal("5"))
    assert ida == idb
    assert db.get_bounty(a, ida)["ref"] != db.get_bounty(b, idb)["ref"]


def test_bad_signature_is_rejected(env):
    client, pp = env
    db.create_bounty(app.conn, "o/r", 7, Decimal("40"))
    assert send(client, merged(), secret="wrong").status_code == 401
    assert pp.paid == []


def test_unmerged_and_other_events_are_ignored(env):
    client, pp = env
    db.create_bounty(app.conn, "o/r", 7, Decimal("40"))
    assert "ignored" in send(client, merged(merged=False)).json()
    assert "ignored" in send(client, merged(), event="push").json()
    assert pp.paid == []


def test_duplicate_delivery_pays_once(env):
    client, pp = env
    db.create_bounty(app.conn, "o/r", 7, Decimal("40"))
    send(client, merged())
    assert send(client, merged()).json() == {"reviewing": []}
    assert len(pp.paid) == 1


def test_manipulation_goes_to_maintainer(env, monkeypatch):
    client, pp = env
    monkeypatch.setattr(app.judge, "judge", lambda *a: verdict(manipulation_attempt=True))
    bid = db.create_bounty(app.conn, "o/r", 7, Decimal("40"))
    send(client, merged())
    row = db.get_bounty(app.conn, bid)
    assert pp.paid == [] and row["status"] == "needs_approval"
    assert "influence the AI" in row["reasons"]


def test_review_failure_goes_to_maintainer(env, monkeypatch):
    client, pp = env
    def broken(*a): raise RuntimeError("AI down")
    monkeypatch.setattr(app.judge, "judge", broken)
    bid = db.create_bounty(app.conn, "o/r", 7, Decimal("40"))
    send(client, merged())
    assert db.get_bounty(app.conn, bid)["status"] == "needs_approval" and pp.paid == []


def test_payout_failure_is_recorded(env, monkeypatch):
    client, _ = env
    monkeypatch.setattr(app, "paypal", FakePayPal(error=PayPalError(422, '{"name": "INSUFFICIENT_FUNDS"}')))
    bid = db.create_bounty(app.conn, "o/r", 7, Decimal("40"))
    send(client, merged())
    assert db.get_bounty(app.conn, bid)["status"] == "failed"
    assert "Payout failed" in db.events(app.conn)[0]["message"]


# ---------- dashboard API ----------

AUTH = ("maintainer", "pw")


def test_dashboard_needs_password(env):
    client, _ = env
    assert client.get("/api/bounties").status_code == 401
    assert client.get("/api/bounties", auth=("x", "wrong")).status_code == 401
    assert client.get("/api/bounties", auth=AUTH).status_code == 200


@pytest.mark.parametrize("payload", [
    {"repo": "not-a-repo", "issue": 7, "amount": "40"},
    {"repo": "o/r", "issue": 0, "amount": "40"},
    {"repo": "o/r", "issue": 7, "amount": "abc"},
    {"repo": "o/r", "issue": 7, "amount": "-5"},
    {"repo": "o/r", "issue": 7, "amount": "1.005"},
    {"repo": "o/r", "issue": 7, "amount": "20000"},
])
def test_create_bounty_validates(env, payload):
    client, _ = env
    assert client.post("/api/bounties", json=payload, auth=AUTH).status_code == 422


def test_create_bounty_once_per_issue(env):
    client, _ = env
    assert client.post("/api/bounties", json={"repo": "o/r", "issue": 7, "amount": "40"}, auth=AUTH).status_code == 200
    assert client.post("/api/bounties", json={"repo": "o/r", "issue": 7, "amount": "9"}, auth=AUTH).status_code == 409
    assert client.get("/api/bounties", auth=AUTH).json()[0]["amount"] == "40.00"


def test_approve_pays_a_queued_bounty(env, monkeypatch):
    client, pp = env
    monkeypatch.setattr(app.judge, "judge", lambda *a: verdict(confidence="medium"))
    bid = db.create_bounty(app.conn, "o/r", 7, Decimal("40"))
    send(client, merged())
    assert db.get_bounty(app.conn, bid)["status"] == "needs_approval" and pp.paid == []
    r = client.post(f"/api/bounties/{bid}/approve", auth=AUTH)
    assert r.status_code == 200 and r.json()["status"] == "paid" and len(pp.paid) == 1
    assert client.post(f"/api/bounties/{bid}/approve", auth=AUTH).status_code == 409  # can't pay twice


def test_approve_needs_a_paypal_email(env, monkeypatch):
    client, pp = env
    pr = merged()
    pr["pull_request"]["user"]["login"] = "stranger"
    bid = db.create_bounty(app.conn, "o/r", 7, Decimal("40"))
    send(client, pr)
    assert client.post(f"/api/bounties/{bid}/approve", auth=AUTH).status_code == 409
    assert client.post("/api/contributors", json={"github_login": "stranger", "paypal_email": "s@example.com"},
                       auth=AUTH).status_code == 200
    assert client.post(f"/api/bounties/{bid}/approve", auth=AUTH).json()["status"] == "paid"


def test_reject(env, monkeypatch):
    client, pp = env
    monkeypatch.setattr(app.judge, "judge", lambda *a: verdict(solves_issue=False))
    bid = db.create_bounty(app.conn, "o/r", 7, Decimal("40"))
    send(client, merged())
    assert client.post(f"/api/bounties/{bid}/reject", auth=AUTH).status_code == 200
    assert db.get_bounty(app.conn, bid)["status"] == "rejected" and pp.paid == []


# ---------- production hardening ----------

def queue(client, monkeypatch, **kw):
    """A merged PR whose review sends the bounty to the maintainer."""
    monkeypatch.setattr(app.judge, "judge", lambda *a: verdict(**{"confidence": "medium", **kw}))
    bid = db.create_bounty(app.conn, "o/r", 7, Decimal("40"))
    send(client, merged())
    return bid


def test_writes_need_the_dashboard_header(env, monkeypatch):
    # A page on another site can make the browser POST with the cached password, but it
    # can't add a custom header. Without it, nothing is paid.
    client, pp = env
    bid = queue(client, monkeypatch)
    bare = TestClient(app.app)
    assert bare.post(f"/api/bounties/{bid}/approve", auth=AUTH).status_code == 403
    assert bare.post(f"/api/bounties/{bid}/reject", auth=AUTH).status_code == 403
    assert bare.post("/api/bounties", json={"repo": "o/r", "issue": 8, "amount": "1"}, auth=AUTH).status_code == 403
    assert pp.paid == [] and db.get_bounty(app.conn, bid)["status"] == "needs_approval"


def test_unexpected_payout_error_never_leaves_a_bounty_stuck(env, monkeypatch):
    client, _ = env
    monkeypatch.setattr(app, "paypal", FakePayPal(error=ValueError("bad response shape")))
    bid = db.create_bounty(app.conn, "o/r", 7, Decimal("40"))
    send(client, merged())
    row = db.get_bounty(app.conn, bid)
    assert row["status"] == "needs_approval" and "check PayPal" in row["reasons"]


def test_no_answer_from_paypal_goes_to_a_human(env, monkeypatch):
    # A timeout may still have created the payout, so it isn't marked failed.
    client, _ = env
    monkeypatch.setattr(app, "paypal", FakePayPal(error=PayPalError(0, "timed out")))
    bid = db.create_bounty(app.conn, "o/r", 7, Decimal("40"))
    send(client, merged())
    row = db.get_bounty(app.conn, bid)
    assert row["status"] == "needs_approval" and "check PayPal" in row["reasons"]


def test_payout_paypal_already_has_is_recorded_not_repeated(env, monkeypatch):
    client, _ = env
    err = PayPalError(400, json.dumps({"name": "USER_BUSINESS_ERROR", "details": [{"field": "SENDER_BATCH_ID",
          "issue": "Batch with given sender_batch_id already exists",
          "link": [{"href": "https://api.sandbox.paypal.com/v1/payments/payouts/OLD123", "rel": "self"}]}]}))
    monkeypatch.setattr(app, "paypal", FakePayPal(error=err))
    bid = db.create_bounty(app.conn, "o/r", 7, Decimal("40"))
    send(client, merged())
    row = db.get_bounty(app.conn, bid)
    assert row["status"] == "paid" and row["payout_batch_id"] == "OLD123"


def test_approve_reports_a_failed_payout(env, monkeypatch):
    client, _ = env
    bid = queue(client, monkeypatch)
    monkeypatch.setattr(app, "paypal", FakePayPal(error=PayPalError(422, '{"name": "INSUFFICIENT_FUNDS"}')))
    r = client.post(f"/api/bounties/{bid}/approve", auth=AUTH)
    assert r.status_code == 502 and "INSUFFICIENT_FUNDS" in r.json()["detail"]
    assert db.get_bounty(app.conn, bid)["status"] == "failed"
    assert client.post(f"/api/bounties/{bid}/reject", auth=AUTH).status_code == 200  # a failed one can be closed


def test_review_uses_the_issue_as_it_was_when_the_bounty_was_posted(env, monkeypatch):
    client, _ = env
    seen = []
    monkeypatch.setattr(app.judge, "judge", lambda *a: seen.append(a[0]) or verdict())
    assert client.post("/api/bounties", json={"repo": "o/r", "issue": 7, "amount": "40"}, auth=AUTH).status_code == 200
    app.github.title = "Rewritten by the PR author to match the PR"
    send(client, merged())
    assert seen == ["Crash on empty name"]


def test_bounty_on_an_unreadable_issue_is_refused(env):
    client, _ = env
    app.github.missing = True
    r = client.post("/api/bounties", json={"repo": "o/r", "issue": 7, "amount": "40"}, auth=AUTH)
    assert r.status_code == 422 and "issue" in r.json()["detail"]
    assert db.bounties(app.conn) == []


def test_rejected_bounty_can_be_reopened_for_the_next_fix(env, monkeypatch):
    client, pp = env
    bid = queue(client, monkeypatch, solves_issue=False)
    client.post(f"/api/bounties/{bid}/reject", auth=AUTH)
    assert client.post(f"/api/bounties/{bid}/reopen", auth=AUTH).status_code == 200
    row = db.get_bounty(app.conn, bid)
    assert row["status"] == "open" and row["pr"] is None and row["verdict"] is None
    monkeypatch.setattr(app.judge, "judge", lambda *a: verdict())
    send(client, merged())
    assert db.get_bounty(app.conn, bid)["status"] == "paid" and len(pp.paid) == 1


def test_returned_payout_becomes_failed_and_can_be_paid_again(env):
    # PayPal accepted the payout, but it never reached anyone (e.g. a mistyped email).
    client, pp = env
    bid = db.create_bounty(app.conn, "o/r", 7, Decimal("40"))
    send(client, merged())
    old_ref = db.get_bounty(app.conn, bid)["ref"]
    pp.status = "RETURNED"
    client.get("/api/bounties", auth=AUTH)  # the dashboard refresh checks PayPal
    row = db.get_bounty(app.conn, bid)
    assert row["status"] == "failed" and row["payout_status"] == "RETURNED"
    assert row["ref"] != old_ref  # no money left, so a retry needs a new PayPal batch
    pp.status = "SUCCESS"
    assert client.post(f"/api/bounties/{bid}/approve", auth=AUTH).json()["status"] == "paid"
    assert len(pp.paid) == 2


def test_successful_payout_status_is_shown(env):
    client, pp = env
    db.create_bounty(app.conn, "o/r", 7, Decimal("40"))
    send(client, merged())
    client.get("/api/bounties", auth=AUTH)
    rows = client.get("/api/bounties", auth=AUTH).json()
    assert rows[0]["payout_status"] == "SUCCESS" and rows[0]["paypal_email"] == "dev@example.com"


def test_config_says_whether_money_is_real(env, monkeypatch):
    client, _ = env
    assert client.get("/api/config", auth=AUTH).json() == {"sandbox": True}
    monkeypatch.setattr(app, "PAYPAL_LIVE", True)
    assert client.get("/api/config", auth=AUTH).json() == {"sandbox": False}


def test_health_check_needs_no_password(env):
    client, _ = env
    r = client.get("/healthz", auth=None)
    assert r.status_code == 200 and r.json() == {"ok": True}
