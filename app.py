"""MergePay web server.

GitHub calls /webhook when a PR is merged. The maintainer uses the dashboard (and the
/api routes behind it), protected by a password.

Run:  uvicorn app:app --reload
"""
import json
import os
import re
import secrets
import sqlite3
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

import db
import judge
from github import GitHub, closing_issues, verify_signature
from paypal import SANDBOX, PayPal, PayPalError
from policy import decide

app = FastAPI(title="MergePay")
conn = db.connect(os.environ.get("DB_PATH", "mergepay.db"))
db.recover_interrupted(conn)  # nothing can be mid-review right after a (re)start
paypal = PayPal.from_env()
PAYPAL_LIVE = os.environ.get("PAYPAL_BASE_URL", SANDBOX).rstrip("/") != SANDBOX
github = GitHub(os.environ["GITHUB_TOKEN"])
WEBHOOK_SECRET = os.environ["GITHUB_WEBHOOK_SECRET"]
ADMIN_PASSWORD = os.environ["ADMIN_PASSWORD"]
os.environ["GEMINI_API_KEY"]  # fail at startup, not at the first review
basic = HTTPBasic()

CHECK_PAYPAL = ("Before approving, check PayPal > Activity. Approving again within 30 days "
                "can't pay twice: PayPal refuses the repeat.")


def admin(creds: HTTPBasicCredentials = Depends(basic)):
    """Browser shows its own login box. Username can be anything; the password must match."""
    if not secrets.compare_digest(creds.password.encode(), ADMIN_PASSWORD.encode()):
        raise HTTPException(401, "Wrong password", headers={"WWW-Authenticate": "Basic"})


def from_dashboard(request: Request):
    """Browsers also send the saved password with form posts from other sites, so a page
    elsewhere could approve a payout. Those pages can't add a custom header; the dashboard
    adds this one to every write."""
    if request.headers.get("X-MergePay") != "1":
        raise HTTPException(403, "Writes must come from the MergePay dashboard.")


WRITE = [Depends(admin), Depends(from_dashboard)]


def to_maintainer(bounty_id: int, reason: str) -> None:
    db.update(conn, bounty_id, status="needs_approval", reasons=[reason])
    db.log(conn, bounty_id, f"Sent to maintainer: {reason}")


# ---------- GitHub webhook ----------

@app.post("/webhook")
async def webhook(request: Request, tasks: BackgroundTasks):
    body = await request.body()
    if not verify_signature(WEBHOOK_SECRET, body, request.headers.get("X-Hub-Signature-256")):
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
                      verdict.manipulation_attempt, email)
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


@app.get("/", dependencies=[Depends(admin)])
def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/api/config", dependencies=[Depends(admin)])
def config():
    return {"sandbox": not PAYPAL_LIVE}


@app.get("/api/bounties", dependencies=[Depends(admin)])
def list_bounties(tasks: BackgroundTasks):
    tasks.add_task(refresh_payouts)
    rows = db.bounties(conn)
    for r in rows:
        r["verdict"] = json.loads(r["verdict"]) if r["verdict"] else None
        r["reasons"] = json.loads(r["reasons"]) if r["reasons"] else []
    return rows


@app.post("/api/bounties", dependencies=WRITE)
def create_bounty(b: BountyIn):
    try:
        amount = Decimal(b.amount)
    except InvalidOperation:
        raise HTTPException(422, "Amount must be a number like 40 or 40.00.")
    if not REPO_RE.match(b.repo) or b.issue < 1:
        raise HTTPException(422, "Use a repo like owner/name and an issue number.")
    if not (0 < amount <= MAX_BOUNTY) or amount.as_tuple().exponent < -2 or b.currency not in {"USD", "EUR", "GBP"}:
        raise HTTPException(422, f"Amount must be between 0.01 and {MAX_BOUNTY} in USD, EUR or GBP.")
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


@app.post("/api/bounties/{bounty_id}/approve", dependencies=WRITE)
def approve(bounty_id: int):
    b = db.get_bounty(conn, bounty_id)
    if not b or b["status"] not in {"needs_approval", "failed"}:
        raise HTTPException(409, "Only bounties waiting for approval (or failed payouts) can be approved.")
    email = db.contributor_email(conn, b["contributor"] or "")
    if not email:
        raise HTTPException(409, f"Register a PayPal email for {b['contributor']} first.")
    if not db.move(conn, bounty_id, ("needs_approval", "failed"), "paying"):
        raise HTTPException(409, "This bounty is already being paid.")
    db.log(conn, bounty_id, "Approved by maintainer")
    pay(bounty_id, email)  # a retry reuses the batch id, so PayPal refuses to pay twice
    b = dict(db.get_bounty(conn, bounty_id))
    if b["status"] != "paid":
        raise HTTPException(502, "The payout did not go through. " + " ".join(json.loads(b["reasons"] or "[]")))
    return b


@app.post("/api/bounties/{bounty_id}/reject", dependencies=WRITE)
def reject(bounty_id: int):
    if not db.move(conn, bounty_id, ("needs_approval", "failed"), "rejected"):
        raise HTTPException(409, "Only bounties waiting for approval (or failed payouts) can be rejected.")
    db.log(conn, bounty_id, "Rejected by maintainer")
    return {"ok": True}


@app.post("/api/bounties/{bounty_id}/reopen", dependencies=WRITE)
def reopen(bounty_id: int):
    """A rejected bounty can wait for the next fix. It keeps its PayPal batch id: if a payout
    was ever sent for it, PayPal refuses a second one."""
    if not db.move(conn, bounty_id, ("rejected",), "open"):
        raise HTTPException(409, "Only rejected bounties can be reopened.")
    db.update(conn, bounty_id, pr=None, contributor=None, verdict=None, reasons=None)
    db.log(conn, bounty_id, "Reopened by maintainer")
    return {"ok": True}


@app.post("/api/contributors", dependencies=WRITE)
def add_contributor(c: ContributorIn):
    # Limit: the maintainer enters emails. Letting contributors self-register safely
    # needs GitHub login (OAuth), otherwise anyone could claim someone else's username.
    if not LOGIN_RE.match(c.github_login) or not EMAIL_RE.match(c.paypal_email):
        raise HTTPException(422, "Enter a GitHub username and a valid email.")
    db.set_contributor(conn, c.github_login, c.paypal_email)
    db.log(conn, None, f"PayPal email saved for {c.github_login}")
    return {"ok": True}


@app.get("/api/events", dependencies=[Depends(admin)])
def list_events():
    return db.events(conn)
