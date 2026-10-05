"""SQLite storage for bounties, contributors and the audit log (stdlib only)."""
import functools
import json
import sqlite3
import threading
from decimal import Decimal

SCHEMA = """
CREATE TABLE IF NOT EXISTS bounties (
    id INTEGER PRIMARY KEY,
    repo TEXT NOT NULL,              -- "owner/name"
    issue INTEGER NOT NULL,
    amount TEXT NOT NULL,            -- Decimal as text, never float
    currency TEXT NOT NULL DEFAULT 'USD',
    status TEXT NOT NULL DEFAULT 'open',  -- open, paying, paid, needs_approval, rejected, failed
    pr INTEGER,
    contributor TEXT,
    verdict TEXT,                    -- the AI reviewer's verdict as JSON
    reasons TEXT,                    -- policy reasons as JSON
    payout_batch_id TEXT,
    -- PayPal sender_batch_id. Random, not the id: ids restart when the database is reset,
    -- and PayPal refuses a sender_batch_id it has already seen.
    ref TEXT NOT NULL DEFAULT (lower(hex(randomblob(8)))),
    -- The issue as it was when the bounty was posted. The review uses this copy, so editing
    -- the issue afterwards to match a PR changes nothing.
    issue_title TEXT,
    issue_body TEXT,
    payout_status TEXT,              -- PayPal's status for the payout item: PENDING, SUCCESS, UNCLAIMED, ...
    checked_at REAL,                 -- when payout_status was last asked from PayPal (unix time)
    UNIQUE (repo, issue)
);
CREATE TABLE IF NOT EXISTS contributors (
    github_login TEXT PRIMARY KEY COLLATE NOCASE,
    paypal_email TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    bounty_id INTEGER REFERENCES bounties(id),
    at TEXT NOT NULL DEFAULT (datetime('now')),
    message TEXT NOT NULL
);
"""

# Web requests and background reviews run on different threads but share one connection.
# Its transaction is shared too, so every call holds this lock: one thread's rollback can't
# undo another thread's write. ponytail: one global lock; fine for one maintainer's traffic.
LOCK = threading.RLock()


def locked(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with LOCK:
            return fn(*args, **kwargs)
    return wrapper


@locked
def connect(path: str = "mergepay.db") -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _upgrade(conn)
    return conn


def _upgrade(conn) -> None:
    """Add columns that databases created by older versions don't have."""
    have = {r["name"] for r in conn.execute("PRAGMA table_info(bounties)")}
    with conn:
        for col in ("ref", "issue_title", "issue_body", "payout_status", "checked_at"):
            if col not in have:
                conn.execute(f"ALTER TABLE bounties ADD COLUMN {col} {'REAL' if col == 'checked_at' else 'TEXT'}")
        if "ref" not in have:
            # The old batch id was "mergepay-<id>". Bounties that may already have reached PayPal
            # keep it, so PayPal refuses to pay them again; the rest get a random ref.
            conn.execute("UPDATE bounties SET ref = CASE WHEN status IN ('paying', 'paid', 'failed') "
                         "THEN CAST(id AS TEXT) ELSE lower(hex(randomblob(8))) END WHERE ref IS NULL")


@locked
def log(conn, bounty_id: int | None, message: str) -> None:
    with conn:
        conn.execute("INSERT INTO events (bounty_id, message) VALUES (?, ?)", (bounty_id, message))


@locked
def create_bounty(conn, repo: str, issue: int, amount: Decimal, currency: str = "USD",
                  issue_title: str | None = None, issue_body: str | None = None) -> int:
    with conn:
        cur = conn.execute("INSERT INTO bounties (repo, issue, amount, currency, issue_title, issue_body) "
                           "VALUES (?, ?, ?, ?, ?, ?)", (repo.lower(), issue, str(amount), currency, issue_title, issue_body))
    log(conn, cur.lastrowid, f"Bounty of {amount} {currency} created on {repo}#{issue}")
    return cur.lastrowid


@locked
def get_bounty(conn, bounty_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM bounties WHERE id = ?", (bounty_id,)).fetchone()


@locked
def open_bounty_for(conn, repo: str, issue: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM bounties WHERE repo = ? AND issue = ? AND status = 'open'",
                        (repo.lower(), issue)).fetchone()


@locked
def move(conn, bounty_id: int, from_statuses: tuple[str, ...], to: str) -> bool:
    """Atomically change the status, only if it is currently one of from_statuses.
    Only one caller can win, so two webhook deliveries or two clicks can never both act."""
    marks = ", ".join("?" * len(from_statuses))
    with conn:
        cur = conn.execute(f"UPDATE bounties SET status = ? WHERE id = ? AND status IN ({marks})",
                           (to, bounty_id, *from_statuses))
    return cur.rowcount == 1


def claim(conn, bounty_id: int, from_status: str = "open") -> bool:
    """Move a bounty to 'paying'. Only one caller can win."""
    return move(conn, bounty_id, (from_status,), "paying")


@locked
def recover_interrupted(conn) -> int:
    """Run at startup: a bounty still 'paying' means the process stopped mid-review or
    mid-payout. A human has to look, because the payout may or may not have been sent."""
    reasons = json.dumps(["MergePay restarted while this bounty was being reviewed or paid. "
                          "Before approving, check PayPal > Activity: approving again within "
                          "30 days can't pay twice, PayPal refuses the repeat."])
    rows = [r["id"] for r in conn.execute("SELECT id FROM bounties WHERE status = 'paying'")]
    with conn:
        conn.execute("UPDATE bounties SET status = 'needs_approval', reasons = ? WHERE status = 'paying'", (reasons,))
    for bid in rows:
        log(conn, bid, "Interrupted by a restart; sent to maintainer")
    return len(rows)


@locked
def update(conn, bounty_id: int, **fields) -> None:
    for key in ("verdict", "reasons"):
        if key in fields and not isinstance(fields[key], str) and fields[key] is not None:
            fields[key] = json.dumps(fields[key])
    cols = ", ".join(f"{k} = ?" for k in fields)  # keys come from our code, never from users
    with conn:
        conn.execute(f"UPDATE bounties SET {cols} WHERE id = ?", (*fields.values(), bounty_id))


@locked
def set_contributor(conn, github_login: str, paypal_email: str) -> None:
    with conn:
        conn.execute("INSERT INTO contributors VALUES (?, ?) "
                     "ON CONFLICT (github_login) DO UPDATE SET paypal_email = excluded.paypal_email",
                     (github_login, paypal_email))


@locked
def contributor_email(conn, github_login: str) -> str | None:
    row = conn.execute("SELECT paypal_email FROM contributors WHERE github_login = ?", (github_login,)).fetchone()
    return row["paypal_email"] if row else None


@locked
def bounties(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT b.*, c.paypal_email FROM bounties b "
        "LEFT JOIN contributors c ON c.github_login = b.contributor ORDER BY b.id DESC")]


@locked
def events(conn, limit: int = 100) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))]
