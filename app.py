"""MergePay web server.

GitHub calls /webhook when a PR is merged. People sign in with GitHub: maintainers connect
their repos and post bounties, contributors save the PayPal email they're paid at. An
optional admin password gives an operator view of everything (self-hosting and tests).

Run:  uvicorn app:app --reload
"""
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

import auth
import db
import judge
from github import GitHub, closing_issues, verify_signature
from paypal import SANDBOX, PayPal, PayPalError
from policy import AUTO_PAY_LIMIT, decide


def require_login_method(oauth, password) -> None:
    if not oauth and not password:
        raise RuntimeError("Set GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET (Sign in with GitHub), "
                           "or ADMIN_PASSWORD, or both.")


log = logging.getLogger("mergepay")
app = FastAPI(title="MergePay")
conn = db.connect(os.environ.get("DB_PATH", "mergepay.db"))
db.recover_interrupted(conn)  # nothing can be mid-review right after a (re)start
paypal = PayPal.from_env()
PAYPAL_LIVE = os.environ.get("PAYPAL_BASE_URL", SANDBOX).rstrip("/") != SANDBOX
github = GitHub(os.environ["GITHUB_TOKEN"])
WEBHOOK_SECRET = os.environ["GITHUB_WEBHOOK_SECRET"]
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD") or None
oauth = (auth.GitHubOAuth(os.environ["GITHUB_CLIENT_ID"], os.environ["GITHUB_CLIENT_SECRET"])
         if os.environ.get("GITHUB_CLIENT_ID") and os.environ.get("GITHUB_CLIENT_SECRET") else None)
require_login_method(oauth, ADMIN_PASSWORD)
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
os.environ["GEMINI_API_KEY"]  # fail at startup, not at the first review
basic = HTTPBasic(auto_error=False)
STATIC = Path(__file__).parent / "static"

CHECK_PAYPAL = ("Before approving, check PayPal > Activity. Approving again within 30 days "
                "can't pay twice: PayPal refuses the repeat.")


@dataclass
class Viewer:
    user: dict | None  # users row (+ github_token) for a GitHub sign-in; None for the operator
    operator: bool = False


def password_ok(creds: HTTPBasicCredentials | None) -> bool:
    return bool(ADMIN_PASSWORD and creds and secrets.compare_digest(creds.password.encode(), ADMIN_PASSWORD.encode()))


def current_viewer(request: Request, creds: HTTPBasicCredentials | None) -> Viewer | None:
    token = request.cookies.get(auth.SESSION_COOKIE)
    if token:
        user = db.session_user(conn, auth.token_hash(token), time.time())
        if user:
            return Viewer(user)
    if password_ok(creds):
        return Viewer(None, operator=True)
    return None


def viewer(request: Request, creds: HTTPBasicCredentials | None = Depends(basic)) -> Viewer:
    """Who is asking: a GitHub sign-in (session cookie) or the operator (admin password)."""
    v = current_viewer(request, creds)
    if not v:
        # Without GitHub sign-in the browser's own password box is the way in.
        headers = None if oauth else {"WWW-Authenticate": "Basic"}
        raise HTTPException(401, "Sign in first.", headers=headers)
    return v


def from_dashboard(request: Request):
    """Browsers also send cookies and the saved password with form posts from other sites, so
    a page elsewhere could approve a payout. Those pages can't add a custom header; the
    dashboard adds this one to every write."""
    if request.headers.get("X-MergePay") != "1":
        raise HTTPException(403, "Writes must come from the MergePay dashboard.")


def writer(v: Viewer = Depends(viewer), _=Depends(from_dashboard)) -> Viewer:
    return v


def repo_secret(github_repo_id: int) -> str:
    """Each connected repo's webhook secret, derived from the server's secret and GitHub's
    numeric repo id. Nothing to store, so it survives a wiped database."""
    return hmac.new(WEBHOOK_SECRET.encode(), f"repo:{github_repo_id}".encode(), hashlib.sha256).hexdigest()


def auto_pay_limit(repo: str) -> Decimal:
    """The repo's own limit if its maintainer set one, never above the operator's AUTO_PAY_LIMIT."""
    r = db.repo_by_name(conn, repo)
    if r and r["auto_pay_limit"] is not None:
        return min(Decimal(r["auto_pay_limit"]), AUTO_PAY_LIMIT)
    return AUTO_PAY_LIMIT


def can_manage(v: Viewer, repo: str) -> bool:
    if v.operator:
        return True
    r = db.repo_by_name(conn, repo)
    return bool(r and r["owner_user_id"] == v.user["id"])


def to_maintainer(bounty_id: int, reason: str) -> None:
    db.update(conn, bounty_id, status="needs_approval", reasons=[reason])
    db.log(conn, bounty_id, f"Sent to maintainer: {reason}")


# ---------- GitHub webhook ----------

@app.post("/webhook")
async def webhook(request: Request, tasks: BackgroundTasks):
    body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")
    ok = verify_signature(WEBHOOK_SECRET, body, signature)
    if not ok:  # a connected repo signs with its own secret; read only the repo id to find it
        try:
            repo_id = int(json.loads(body)["repository"]["id"])
        except (ValueError, KeyError, TypeError):
            repo_id = None
        ok = repo_id is not None and verify_signature(repo_secret(repo_id), body, signature)
    if not ok:
        raise HTTPException(401, "Bad signature")
    if request.headers.get("X-GitHub-Event") != "pull_request":
        return {"ignored": "not a pull_request event"}
    event = await request.json()
    pr = event["pull_request"]
    if event.get("action") != "closed" or not pr.get("merged"):
        return {"ignored": "PR not merged"}
    # Like GitHub, only count a fix once it lands on the default branch.
    if pr["base"]["ref"] != event["repository"]["default_branch"]:
        return {"ignored": "PR not merged into the default branch"}

    repo = event["repository"]["full_name"]
    started = []
    for issue in closing_issues(pr.get("body")):
        bounty = db.open_bounty_for(conn, repo, issue)
        # claim() lets only one delivery process a bounty, even if GitHub retries.
        if bounty and db.claim(conn, bounty["id"]):
            db.update(conn, bounty["id"], pr=pr["number"], contributor=pr["user"]["login"])
            db.log(conn, bounty["id"], f"PR #{pr['number']} by {pr['user']['login']} merged; reviewing")
            # GitHub gives up on a webhook after 10 seconds, so the slow AI review runs after we reply.
            tasks.add_task(review_and_pay, bounty["id"], repo, issue, pr)
            started.append(issue)
    return {"reviewing": started}


def review_and_pay(bounty_id: int, repo: str, issue_number: int, pr: dict) -> None:
    try:
        _review_and_pay(bounty_id, repo, issue_number, pr)
    except Exception as e:  # never leave a bounty stuck in 'paying'
        to_maintainer(bounty_id, f"Something went wrong ({e}). {CHECK_PAYPAL}")


def _review_and_pay(bounty_id: int, repo: str, issue_number: int, pr: dict) -> None:
    bounty = db.get_bounty(conn, bounty_id)
    try:
        if bounty["issue_title"] is not None:  # the issue as it was when the bounty was posted
            issue = {"title": bounty["issue_title"], "body": bounty["issue_body"] or ""}
        else:  # bounties created before issues were saved
            issue = github.issue(repo, issue_number)
        diff = github.pr_diff(repo, pr["number"])
        verdict = judge.judge(issue["title"], issue["body"], pr["title"], pr.get("body") or "", diff)
    except Exception as e:  # GitHub or AI failed: hand it to the maintainer instead of guessing
        to_maintainer(bounty_id, f"Automatic review failed: {e}")
        return

    email = db.contributor_email(conn, pr["user"]["login"])
    decision = decide(Decimal(bounty["amount"]), verdict.solves_issue, verdict.confidence,
                      verdict.manipulation_attempt, email, limit=auto_pay_limit(repo))
    db.update(conn, bounty_id, verdict=verdict.model_dump(), reasons=decision.reasons)
    db.log(conn, bounty_id, f"AI review: {verdict.summary} ({verdict.confidence} confidence)")
    if decision.action == "pay":
        pay(bounty_id, email)
    else:
        db.update(conn, bounty_id, status="needs_approval")
        db.log(conn, bounty_id, "Sent to maintainer: " + " ".join(decision.reasons))


def pay(bounty_id: int, email: str) -> None:
    """Pay a bounty that has already been claimed (status 'paying'). Always leaves it in
    'paid', 'failed' or 'needs_approval'."""
    b = db.get_bounty(conn, bounty_id)
    try:
        header = paypal.pay(f"mergepay-{b['ref']}", email, b["amount"], b["currency"],
                            f"Bounty for {b['repo']}#{b['issue']} (PR #{b['pr']}). Thank you!")
    except PayPalError as e:
        if e.existing_batch:  # PayPal already has this payout: record it, never send it again
            db.update(conn, bounty_id, status="paid", payout_batch_id=e.existing_batch, payout_status=None)
            db.log(conn, bounty_id, f"PayPal already had this payout (batch {e.existing_batch}); nothing was sent twice")
        elif e.status == 0 or e.status >= 500:  # no clear answer: the payout may exist
            to_maintainer(bounty_id, f"PayPal didn't answer clearly ({e}). {CHECK_PAYPAL}")
        else:
            db.update(conn, bounty_id, status="failed", reasons=[f"PayPal refused the payout: {e}"])
            db.log(conn, bounty_id, f"Payout failed: {e}")
        return
    except Exception as e:
        to_maintainer(bounty_id, f"Unexpected error while paying ({e}). {CHECK_PAYPAL}")
        return
    db.update(conn, bounty_id, status="paid", payout_batch_id=header["payout_batch_id"],
              payout_status=header.get("batch_status"))
    db.log(conn, bounty_id, f"Paid {b['amount']} {b['currency']} to {b['contributor']} "
                            f"(PayPal batch {header['payout_batch_id']})")


# PayPal item statuses that mean the money never reached (or left) the contributor.
NOT_DELIVERED = {"FAILED", "RETURNED", "BLOCKED", "DENIED", "REFUNDED", "REVERSED"}
CHECK_EVERY = 60  # seconds between PayPal status checks per payout


def refresh_payouts() -> None:
    """'paid' means PayPal accepted the payout. It can still come back, e.g. when nobody has a
    PayPal account at that email (unclaimed payouts return after 30 days). Ask PayPal again.
    ponytail: runs when the dashboard refreshes, not on a timer; add a scheduler if nobody
    opens the dashboard for weeks."""
    now = time.time()
    for b in db.bounties(conn):
        if b["status"] != "paid" or not b["payout_batch_id"] or b["payout_status"] == "SUCCESS":
            continue
        if b["checked_at"] and now - b["checked_at"] < CHECK_EVERY:
            continue
        db.update(conn, b["id"], checked_at=now)
        try:
            status = paypal.payout_status(b["payout_batch_id"])
        except Exception:
            continue  # try again on a later refresh
        db.update(conn, b["id"], payout_status=status)
        if status in NOT_DELIVERED:
            # No money left the account, so a retry is a new payout and needs a new batch id.
            db.update(conn, b["id"], status="failed", ref=secrets.token_hex(8), reasons=[
                f"PayPal could not deliver the payout ({status}). Check the contributor's PayPal email, then retry."])
            db.log(conn, b["id"], f"PayPal could not deliver the payout ({status})")


# ---------- Maintainer dashboard ----------

class BountyIn(BaseModel):
    repo: str
    issue: int
    amount: str
    currency: str = "USD"


class ContributorIn(BaseModel):
    github_login: str
    paypal_email: str


REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$")
LOGIN_RE = re.compile(r"^[A-Za-z0-9-]{1,39}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_BOUNTY = Decimal("10000")


@app.get("/healthz")
def healthz():
    """For Render's health check: no password, no secrets, no database writes."""
    return {"ok": True}


# ---------- Pages and sign in ----------

def public_url(request: Request) -> str:
    return PUBLIC_URL or str(request.base_url).rstrip("/")


def set_cookie(response, request: Request, name: str, value: str, max_age: int) -> None:
    response.set_cookie(name, value, max_age=max_age, httponly=True, samesite="lax",
                        secure=public_url(request).startswith("https://"))


@app.get("/")
def index(request: Request, creds: HTTPBasicCredentials | None = Depends(basic)):
    if not current_viewer(request, creds):
        if oauth:
            return RedirectResponse("/signin")
        raise HTTPException(401, "Sign in first.", headers={"WWW-Authenticate": "Basic"})
    return FileResponse(STATIC / "index.html")


@app.get("/signin")
def signin():
    return FileResponse(STATIC / "signin.html")


@app.get("/auth/options")
def auth_options():
    return {"oauth": bool(oauth), "password": bool(ADMIN_PASSWORD)}


@app.get("/operator")
def operator(creds: HTTPBasicCredentials | None = Depends(basic)):
    """The operator's way in: the browser asks for the admin password, then opens the dashboard."""
    if not password_ok(creds):
        raise HTTPException(401, "Wrong password", headers={"WWW-Authenticate": "Basic"})
    return RedirectResponse("/")


@app.get("/auth/login")
def login(request: Request):
    if not oauth:
        raise HTTPException(404, "Sign in with GitHub is not set up on this server.")
    state = secrets.token_urlsafe(24)
    response = RedirectResponse(oauth.authorize_url(public_url(request) + "/auth/callback", state))
    set_cookie(response, request, auth.STATE_COOKIE, state, 600)
    return response


@app.get("/auth/callback")
def callback(request: Request, code: str = "", state: str = "", error: str = ""):
    """GitHub sends the browser back here. The state cookie proves this sign-in started in this
    browser, so nobody can sign you in to their account with a forged link."""
    expected = request.cookies.get(auth.STATE_COOKIE, "")
    if not oauth or error or not code or not state or not expected or not secrets.compare_digest(state, expected):
        return RedirectResponse("/signin?error=" + ("denied" if error else "state"))
    redirect_uri = public_url(request) + "/auth/callback"
    try:
        token = oauth.exchange(code, redirect_uri)
        user = oauth.user(token)
        user_id = db.upsert_user(conn, int(user["id"]), user["login"], user.get("avatar_url"))
    except (httpx.HTTPError, auth.OAuthError, KeyError, ValueError) as e:
        # GitHub's reason (e.g. "incorrect_client_credentials") goes to the server log only;
        # it holds no secrets, and it's what an operator needs to fix the setup.
        log.warning("GitHub sign-in failed: %s: %s", type(e).__name__, e)
        return RedirectResponse("/signin?error=github")
    session = auth.new_token()
    now = time.time()
    db.create_session(conn, auth.token_hash(session), user_id, token, now, now + auth.SESSION_SECONDS)
    response = RedirectResponse("/", status_code=303)
    set_cookie(response, request, auth.SESSION_COOKIE, session, auth.SESSION_SECONDS)
    response.delete_cookie(auth.STATE_COOKIE)
    return response


@app.post("/auth/logout", dependencies=[Depends(from_dashboard)])
def logout(request: Request):
    token = request.cookies.get(auth.SESSION_COOKIE)
    if token:
        db.delete_session(conn, auth.token_hash(token))  # also forgets the GitHub token
    response = JSONResponse({"ok": True})
    response.delete_cookie(auth.SESSION_COOKIE)
    return response


# ---------- Profile and repositories ----------

class PayPalIn(BaseModel):
    paypal_email: str


class RepoIn(BaseModel):
    full_name: str


class LimitIn(BaseModel):
    auto_pay_limit: str | None


def repo_out(r: dict) -> dict:
    return {"id": r["id"], "full_name": r["full_name"], "auto_pay_limit": r["auto_pay_limit"]}


@app.get("/api/me")
def me(v: Viewer = Depends(viewer)):
    common = {"operator": v.operator, "auto_pay_cap": str(AUTO_PAY_LIMIT), "oauth": bool(oauth)}
    if v.operator:
        return {**common, "login": None, "avatar_url": None, "paypal_email": None,
                "repos": [repo_out(r) for r in db.repos(conn)]}
    u = v.user
    return {**common, "login": u["login"], "avatar_url": u["avatar_url"],
            "paypal_email": db.contributor_email(conn, u["login"]),
            "repos": [repo_out(r) for r in db.repos(conn, u["id"])]}


@app.post("/api/me/paypal")
def save_my_paypal(p: PayPalIn, v: Viewer = Depends(writer)):
    if v.operator:
        raise HTTPException(403, "The operator sets contributor emails from the dashboard.")
    if not EMAIL_RE.match(p.paypal_email.strip()):
        raise HTTPException(422, "Enter a valid email, like name@example.com.")
    db.set_contributor(conn, v.user["login"], p.paypal_email.strip())
    db.log(conn, None, f"PayPal email saved for {v.user['login']}")
    return {"ok": True}


@app.post("/api/repos")
def connect_repo(r: RepoIn, v: Viewer = Depends(writer)):
    if v.operator or not oauth:
        raise HTTPException(403, "Sign in with GitHub to connect a repository.")
    name = r.full_name.strip()
    if not REPO_RE.match(name):
        raise HTTPException(422, "Use a repository like owner/name.")
    try:
        info = oauth.repo(v.user["github_token"], name)
    except auth.OAuthError:
        raise HTTPException(401, "Your GitHub sign-in has expired. Sign in again.")
    except httpx.HTTPError:
        raise HTTPException(502, "Couldn't reach GitHub. Try again in a moment.")
    if not info:
        raise HTTPException(404, f"GitHub can't find {name}. Check the name; private repositories aren't supported yet.")
    perms = info.get("permissions") or {}
    if not (perms.get("admin") or perms.get("maintain")):
        raise HTTPException(403, f"You need admin or maintain access to {info['full_name']} on GitHub to connect it.")
    try:
        repo_id = db.add_repo(conn, info["full_name"], int(info["id"]), v.user["id"])
    except sqlite3.IntegrityError:
        raise HTTPException(409, f"{info['full_name']} is already connected to MergePay.")
    db.log(conn, None, f"{v.user['login']} connected {info['full_name']}")
    return repo_out(db.get_repo(conn, repo_id))


def owned_repo(repo_id: int, v: Viewer) -> dict:
    r = db.get_repo(conn, repo_id)
    if not r or not (v.operator or r["owner_user_id"] == v.user["id"]):
        raise HTTPException(404, "No such repository.")
    return r


@app.get("/api/repos/{repo_id}/webhook")
def repo_webhook(repo_id: int, request: Request, v: Viewer = Depends(viewer)):
    r = owned_repo(repo_id, v)
    return {"payload_url": public_url(request) + "/webhook", "content_type": "application/json",
            "events": ["pull_request"], "secret": repo_secret(r["github_repo_id"])}


@app.post("/api/repos/{repo_id}/limit")
def set_repo_limit(repo_id: int, body: LimitIn, v: Viewer = Depends(writer)):
    owned_repo(repo_id, v)
    limit = None
    if body.auto_pay_limit not in (None, ""):
        try:
            limit = Decimal(body.auto_pay_limit)
        except InvalidOperation:
            raise HTTPException(422, "Enter an amount like 20 or 20.00.")
        if not (0 <= limit <= AUTO_PAY_LIMIT) or limit.as_tuple().exponent < -2:
            raise HTTPException(422, f"The auto-pay limit must be between 0 and {AUTO_PAY_LIMIT}.")
        limit = str(limit.quantize(Decimal("0.01")))
    db.set_repo_limit(conn, repo_id, limit)
    return {"ok": True, "auto_pay_limit": limit}


@app.delete("/api/repos/{repo_id}")
def disconnect_repo(repo_id: int, v: Viewer = Depends(writer)):
    r = owned_repo(repo_id, v)
    db.delete_repo(conn, repo_id)
    db.log(conn, None, f"{r['full_name']} disconnected")
    return {"ok": True}


# ---------- Bounties ----------

@app.get("/api/config")
def config(v: Viewer = Depends(viewer)):
    return {"sandbox": not PAYPAL_LIVE}


def visible_bounties(v: Viewer) -> list[dict]:
    if v.operator:
        rows, mine = db.bounties(conn), None
    else:
        rows = db.bounties_for_user(conn, v.user["id"], v.user["login"])
        mine = {r["full_name"] for r in db.repos(conn, v.user["id"])}
    for r in rows:
        r["verdict"] = json.loads(r["verdict"]) if r["verdict"] else None
        r["reasons"] = json.loads(r["reasons"]) if r["reasons"] else []
        r["can_manage"] = mine is None or r["repo"] in mine
    return rows


def managed_bounty(bounty_id: int, v: Viewer):
    """A bounty this viewer may act on. Others get 404, so nobody learns what exists elsewhere."""
    b = db.get_bounty(conn, bounty_id)
    if not b or not can_manage(v, b["repo"]):
        raise HTTPException(404, "No such bounty.")
    return b


@app.get("/api/bounties")
def list_bounties(tasks: BackgroundTasks, v: Viewer = Depends(viewer)):
    tasks.add_task(refresh_payouts)
    return visible_bounties(v)


@app.post("/api/bounties")
def create_bounty(b: BountyIn, v: Viewer = Depends(writer)):
    try:
        amount = Decimal(b.amount)
    except InvalidOperation:
        raise HTTPException(422, "Amount must be a number like 40 or 40.00.")
    if not REPO_RE.match(b.repo) or b.issue < 1:
        raise HTTPException(422, "Use a repo like owner/name and an issue number.")
    if not (0 < amount <= MAX_BOUNTY) or amount.as_tuple().exponent < -2 or b.currency not in {"USD", "EUR", "GBP"}:
        raise HTTPException(422, f"Amount must be between 0.01 and {MAX_BOUNTY} in USD, EUR or GBP.")
    if not can_manage(v, b.repo):
        raise HTTPException(403, f"Connect {b.repo} in Settings before posting bounties on it.")
    try:
        issue = github.issue(b.repo, b.issue)
    except httpx.HTTPError:
        raise HTTPException(422, f"Can't read issue {b.repo}#{b.issue}. Check the repo and number, "
                                 "and that the GitHub token can read this repository.")
    try:
        return {"id": db.create_bounty(conn, b.repo, b.issue, amount.quantize(Decimal("0.01")), b.currency,
                                       issue["title"], issue["body"])}
    except sqlite3.IntegrityError:
        raise HTTPException(409, "That issue already has a bounty.")


@app.post("/api/bounties/{bounty_id}/approve")
def approve(bounty_id: int, v: Viewer = Depends(writer)):
    b = managed_bounty(bounty_id, v)
    if b["status"] not in {"needs_approval", "failed"}:
        raise HTTPException(409, "Only bounties waiting for approval (or failed payouts) can be approved.")
    email = db.contributor_email(conn, b["contributor"] or "")
    if not email:
        raise HTTPException(409, f"{b['contributor']} hasn't saved a PayPal email yet. They can add it in Settings.")
    if not db.move(conn, bounty_id, ("needs_approval", "failed"), "paying"):
        raise HTTPException(409, "This bounty is already being paid.")
    db.log(conn, bounty_id, "Approved by maintainer")
    pay(bounty_id, email)  # a retry reuses the batch id, so PayPal refuses to pay twice
    b = dict(db.get_bounty(conn, bounty_id))
    if b["status"] != "paid":
        raise HTTPException(502, "The payout did not go through. " + " ".join(json.loads(b["reasons"] or "[]")))
    return b


@app.post("/api/bounties/{bounty_id}/reject")
def reject(bounty_id: int, v: Viewer = Depends(writer)):
    managed_bounty(bounty_id, v)
    if not db.move(conn, bounty_id, ("needs_approval", "failed"), "rejected"):
        raise HTTPException(409, "Only bounties waiting for approval (or failed payouts) can be rejected.")
    db.log(conn, bounty_id, "Rejected by maintainer")
    return {"ok": True}


@app.post("/api/bounties/{bounty_id}/reopen")
def reopen(bounty_id: int, v: Viewer = Depends(writer)):
    """A rejected bounty can wait for the next fix. It keeps its PayPal batch id: if a payout
    was ever sent for it, PayPal refuses a second one."""
    managed_bounty(bounty_id, v)
    if not db.move(conn, bounty_id, ("rejected",), "open"):
        raise HTTPException(409, "Only rejected bounties can be reopened.")
    db.update(conn, bounty_id, pr=None, contributor=None, verdict=None, reasons=None)
    db.log(conn, bounty_id, "Reopened by maintainer")
    return {"ok": True}


@app.post("/api/contributors")
def add_contributor(c: ContributorIn, v: Viewer = Depends(writer)):
    """The operator can set anyone's email. Signed-in contributors save their own in Settings,
    where GitHub has already proved who they are."""
    if not v.operator:
        raise HTTPException(403, "Contributors add their own PayPal email in Settings.")
    if not LOGIN_RE.match(c.github_login) or not EMAIL_RE.match(c.paypal_email):
        raise HTTPException(422, "Enter a GitHub username and a valid email.")
    db.set_contributor(conn, c.github_login, c.paypal_email)
    db.log(conn, None, f"PayPal email saved for {c.github_login}")
    return {"ok": True}


@app.get("/api/events")
def list_events(v: Viewer = Depends(viewer)):
    if v.operator:
        return db.events(conn)
    return db.events_for(conn, [b["id"] for b in visible_bounties(v)], v.user["login"])
