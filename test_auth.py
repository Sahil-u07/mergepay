"""Sign in with GitHub, sessions, connected repos and who may see or touch what.
GitHub's OAuth and REST calls are faked; the rest of the app is real."""
import hashlib
import hmac
import json
import time
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from test_app import AUTH, FakeGitHub, FakePayPal, verdict  # sets the test environment first
import app
import auth
import db


class FakeOAuth:
    """Stands in for github.com: code "alice" signs in as alice, "bob" as bob."""
    def __init__(self):
        self.users = {"alice": {"id": 101, "login": "alice", "avatar_url": "https://a.example/a.png"},
                      "bob": {"id": 202, "login": "bob", "avatar_url": None}}
        self.perms = {("alice", "alice/app"): {"admin": True}, ("bob", "alice/app"): {"pull": True},
                      ("bob", "bob/lib"): {"maintain": True}}
        self.repo_ids = {"alice/app": 9001, "bob/lib": 9002}
        self.fail_exchange = False

    def authorize_url(self, redirect_uri, state):
        return f"https://github.example/login/oauth/authorize?state={state}&redirect_uri={redirect_uri}"

    def exchange(self, code, redirect_uri):
        if self.fail_exchange:
            raise auth.OAuthError("bad_verification_code")
        return "token-" + code

    def user(self, token):
        return self.users[token.removeprefix("token-")]

    def repo(self, token, full_name):
        name = full_name.lower()
        if name not in self.repo_ids:
            return None
        login = token.removeprefix("token-")
        return {"id": self.repo_ids[name], "full_name": full_name,
                "permissions": self.perms.get((login, name), {"pull": True})}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(app, "conn", db.connect(":memory:"))
    monkeypatch.setattr(app, "github", FakeGitHub())
    monkeypatch.setattr(app, "oauth", FakeOAuth())
    pp = FakePayPal()
    monkeypatch.setattr(app, "paypal", pp)
    monkeypatch.setattr(app.judge, "judge", lambda *a: verdict())
    return pp


def client():
    return TestClient(app.app, headers={"X-MergePay": "1"}, follow_redirects=False)


def sign_in(code):
    c = client()
    r = c.get("/auth/login")
    assert r.status_code in (302, 307)
    state = r.cookies.get(auth.STATE_COOKIE) or c.cookies.get(auth.STATE_COOKIE)
    r = c.get("/auth/callback", params={"code": code, "state": state})
    assert r.status_code in (302, 303) and r.headers["location"] == "/"
    return c


def connect(c, repo):
    return c.post("/api/repos", json={"full_name": repo})


def signed(payload, secret):
    body = json.dumps(payload).encode()
    return body, "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def merged_payload(repo="alice/app", repo_id=9001, issue=7, user="dev"):
    return {"action": "closed", "repository": {"id": repo_id, "full_name": repo, "default_branch": "main"},
            "pull_request": {"number": 12, "merged": True, "title": "Fix", "body": f"Fixes #{issue}",
                             "user": {"login": user}, "base": {"ref": "main"}}}


# ---------- sign in ----------

def test_login_redirects_to_github_with_a_state_cookie(env):
    c = client()
    r = c.get("/auth/login")
    assert r.headers["location"].startswith("https://github.example/login/oauth/authorize")
    assert "/auth/callback" in r.headers["location"]
    assert c.cookies.get(auth.STATE_COOKIE)


def test_callback_signs_in_and_me_shows_the_user(env):
    c = sign_in("alice")
    me = c.get("/api/me").json()
    assert me["login"] == "alice" and me["operator"] is False and me["repos"] == []
    assert "github_token" not in json.dumps(me)


@pytest.mark.parametrize("state", ["", "forged"])
def test_callback_with_a_bad_state_is_refused(env, state):
    c = client()
    c.get("/auth/login")
    r = c.get("/auth/callback", params={"code": "alice", "state": state})
    assert r.headers["location"].startswith("/signin?error=")
    assert c.get("/api/me").status_code == 401


def test_github_errors_send_you_back_to_sign_in(env):
    app.oauth.fail_exchange = True
    c = client()
    state = c.get("/auth/login").cookies.get(auth.STATE_COOKIE) or c.cookies.get(auth.STATE_COOKIE)
    r = c.get("/auth/callback", params={"code": "alice", "state": state})
    assert r.headers["location"] == "/signin?error=github"
    r = c.get("/auth/callback", params={"error": "access_denied", "state": state})
    assert r.headers["location"].startswith("/signin?error=")


def test_session_expires(env):
    c = sign_in("alice")
    with app.conn:
        app.conn.execute("UPDATE sessions SET expires_at = ?", (time.time() - 1,))
    assert c.get("/api/me").status_code == 401


def test_sign_out_ends_the_session(env):
    c = sign_in("alice")
    assert c.post("/auth/logout").status_code == 200
    assert c.get("/api/me").status_code == 401
    assert app.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_signed_out_browsers_go_to_the_sign_in_page(env):
    r = client().get("/")
    assert r.status_code in (302, 307) and r.headers["location"] == "/signin"
    assert client().get("/signin").status_code == 200
    assert client().get("/auth/options").json() == {"oauth": True, "password": True}


def test_operator_password_still_works(env):
    c = client()
    assert c.get("/api/me", auth=AUTH).json()["operator"] is True
    assert c.get("/api/bounties", auth=AUTH).status_code == 200


def test_app_needs_a_way_to_sign_in():
    with pytest.raises(RuntimeError):
        app.require_login_method(oauth=None, password=None)
    app.require_login_method(oauth=object(), password=None)
    app.require_login_method(oauth=None, password="pw")


# ---------- repos ----------

def test_connect_a_repo_you_maintain(env):
    c = sign_in("alice")
    r = connect(c, "alice/app")
    assert r.status_code == 200 and r.json()["full_name"] == "alice/app"
    assert [x["full_name"] for x in c.get("/api/me").json()["repos"]] == ["alice/app"]


def test_cant_connect_a_repo_you_dont_maintain(env):
    c = sign_in("bob")
    assert connect(c, "alice/app").status_code == 403
    assert connect(c, "nobody/missing").status_code == 404
    assert connect(c, "not a repo").status_code == 422


def test_a_repo_has_one_maintainer_in_mergepay(env):
    connect(sign_in("alice"), "alice/app")
    app.oauth.perms[("bob", "alice/app")] = {"admin": True}
    assert connect(sign_in("bob"), "alice/app").status_code == 409


def test_only_the_owner_sees_the_webhook_secret(env):
    alice = sign_in("alice")
    rid = connect(alice, "alice/app").json()["id"]
    hook = alice.get(f"/api/repos/{rid}/webhook").json()
    assert hook["payload_url"].endswith("/webhook") and len(hook["secret"]) == 64
    assert sign_in("bob").get(f"/api/repos/{rid}/webhook").status_code == 404


def test_per_repo_webhook_secret_is_accepted(env):
    alice = sign_in("alice")
    rid = connect(alice, "alice/app").json()["id"]
    secret = alice.get(f"/api/repos/{rid}/webhook").json()["secret"]
    assert alice.post("/api/bounties", json={"repo": "alice/app", "issue": 7, "amount": "20"}).status_code == 200
    db.set_contributor(app.conn, "dev", "dev@example.com")
    body, sig = signed(merged_payload(), secret)
    r = client().post("/webhook", content=body, headers={"X-Hub-Signature-256": sig, "X-GitHub-Event": "pull_request"})
    assert r.json() == {"reviewing": [7]}
    assert env.paid  # small, clean fix: paid automatically

    # Another repo's secret, or garbage, is refused.
    body, sig = signed(merged_payload(repo="bob/lib", repo_id=9002), secret)
    assert client().post("/webhook", content=body, headers={"X-Hub-Signature-256": sig,
                                                            "X-GitHub-Event": "pull_request"}).status_code == 401
    assert client().post("/webhook", content=b"not json", headers={"X-Hub-Signature-256": "sha256=00"}).status_code == 401


def test_repo_auto_pay_limit(env):
    alice = sign_in("alice")
    rid = connect(alice, "alice/app").json()["id"]
    assert alice.post(f"/api/repos/{rid}/limit", json={"auto_pay_limit": "51"}).status_code == 422  # above the cap
    assert alice.post(f"/api/repos/{rid}/limit", json={"auto_pay_limit": "1.005"}).status_code == 422
    assert alice.post(f"/api/repos/{rid}/limit", json={"auto_pay_limit": "10"}).status_code == 200
    alice.post("/api/bounties", json={"repo": "alice/app", "issue": 7, "amount": "20"})
    db.set_contributor(app.conn, "dev", "dev@example.com")
    body, sig = signed(merged_payload(), "whsec")
    client().post("/webhook", content=body, headers={"X-Hub-Signature-256": sig, "X-GitHub-Event": "pull_request"})
    b = alice.get("/api/bounties").json()[0]
    assert b["status"] == "needs_approval" and "auto-pay limit of 10" in " ".join(b["reasons"])
    assert alice.post(f"/api/repos/{rid}/limit", json={"auto_pay_limit": None}).status_code == 200


def test_disconnect_a_repo(env):
    alice = sign_in("alice")
    rid = connect(alice, "alice/app").json()["id"]
    assert sign_in("bob").delete(f"/api/repos/{rid}").status_code == 404
    assert alice.delete(f"/api/repos/{rid}").status_code == 200
    assert alice.get("/api/me").json()["repos"] == []


# ---------- who sees and touches what ----------

def test_maintainers_only_see_and_act_on_their_own_bounties(env):
    alice, bob = sign_in("alice"), sign_in("bob")
    connect(alice, "alice/app")
    assert bob.post("/api/bounties", json={"repo": "alice/app", "issue": 7, "amount": "20"}).status_code == 403
    alice.post("/api/bounties", json={"repo": "alice/app", "issue": 7, "amount": "200"})
    bid = alice.get("/api/bounties").json()[0]["id"]
    db.update(app.conn, bid, status="needs_approval", contributor="dev")
    assert alice.get("/api/bounties").json()[0]["can_manage"] is True
    assert bob.get("/api/bounties").json() == []
    assert bob.get("/api/events").json() == []
    for action in ("approve", "reject", "reopen"):
        assert bob.post(f"/api/bounties/{bid}/{action}").status_code == 404
    assert alice.post(f"/api/bounties/{bid}/reject").status_code == 200


def test_contributors_see_their_payouts_read_only(env):
    alice = sign_in("alice")
    connect(alice, "alice/app")
    alice.post("/api/bounties", json={"repo": "alice/app", "issue": 7, "amount": "20"})
    bid = alice.get("/api/bounties").json()[0]["id"]
    db.update(app.conn, bid, status="needs_approval", contributor="Bob")
    bob = sign_in("bob")
    mine = bob.get("/api/bounties").json()
    assert [b["id"] for b in mine] == [bid] and mine[0]["can_manage"] is False
    assert bob.post(f"/api/bounties/{bid}/approve").status_code == 404


def test_users_save_their_own_paypal_email(env):
    bob = sign_in("bob")
    assert bob.post("/api/me/paypal", json={"paypal_email": "nope"}).status_code == 422
    assert bob.post("/api/me/paypal", json={"paypal_email": "bob@example.com"}).status_code == 200
    assert bob.get("/api/me").json()["paypal_email"] == "bob@example.com"
    assert db.contributor_email(app.conn, "bob") == "bob@example.com"
    # Setting someone else's email is for the operator only.
    assert bob.post("/api/contributors", json={"github_login": "alice", "paypal_email": "x@example.com"}).status_code == 403
    assert client().post("/api/contributors", json={"github_login": "alice", "paypal_email": "x@example.com"},
                         auth=AUTH).status_code == 200


def test_writes_still_need_the_dashboard_header(env):
    alice = sign_in("alice")
    bare = TestClient(app.app, cookies=alice.cookies, follow_redirects=False)
    assert bare.post("/api/repos", json={"full_name": "alice/app"}).status_code == 403
    assert bare.post("/auth/logout").status_code == 403


def test_revoked_github_token_asks_you_to_sign_in_again(env):
    alice = sign_in("alice")
    def revoked(token, full_name):
        raise auth.OAuthError("token revoked")
    app.oauth.repo = revoked
    r = connect(alice, "alice/app")
    assert r.status_code == 401 and "sign in again" in r.json()["detail"].lower()
