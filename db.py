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
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    github_id INTEGER NOT NULL UNIQUE,  -- never changes, unlike the login
    login TEXT NOT NULL,
    avatar_url TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,        -- sha256 of the cookie value
    user_id INTEGER NOT NULL REFERENCES users(id),
    github_token TEXT,                  -- no-scope OAuth token, used only to check repo permissions
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS repos (
    id INTEGER PRIMARY KEY,
    full_name TEXT NOT NULL UNIQUE,     -- lowercase "owner/name", like bounties.repo
    github_repo_id INTEGER NOT NULL UNIQUE,
    owner_user_id INTEGER NOT NULL REFERENCES users(id),
    auto_pay_limit TEXT,                -- Decimal as text; NULL = the server's AUTO_PAY_LIMIT
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
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


# ---------- Accounts ----------

@locked
def upsert_user(conn, github_id: int, login: str, avatar_url: str | None) -> int:
    with conn:
        conn.execute("INSERT INTO users (github_id, login, avatar_url) VALUES (?, ?, ?) ON CONFLICT (github_id) "
                     "DO UPDATE SET login = excluded.login, avatar_url = excluded.avatar_url",
                     (github_id, login, avatar_url))
    return conn.execute("SELECT id FROM users WHERE github_id = ?", (github_id,)).fetchone()["id"]


@locked
def create_session(conn, token_hash: str, user_id: int, github_token: str, now: float, expires_at: float) -> None:
    with conn:
        conn.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?)", (token_hash, user_id, github_token, now, expires_at))


@locked
def session_user(conn, token_hash: str, now: float) -> dict | None:
    row = conn.execute("SELECT u.*, s.github_token FROM sessions s JOIN users u ON u.id = s.user_id "
                       "WHERE s.token_hash = ? AND s.expires_at > ?", (token_hash, now)).fetchone()
    return dict(row) if row else None


@locked
def delete_session(conn, token_hash: str) -> None:
    with conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))


@locked
def add_repo(conn, full_name: str, github_repo_id: int, owner_user_id: int) -> int:
    """Raises sqlite3.IntegrityError if the repo is already connected."""
    with conn:
        cur = conn.execute("INSERT INTO repos (full_name, github_repo_id, owner_user_id) VALUES (?, ?, ?)",
                           (full_name.lower(), github_repo_id, owner_user_id))
    return cur.lastrowid


@locked
def repos(conn, owner_user_id: int | None = None) -> list[dict]:
    """Repos one user connected, or all of them (operator)."""
    sql = "SELECT id, full_name, github_repo_id, owner_user_id, auto_pay_limit FROM repos"
    args = ()
    if owner_user_id is not None:
        sql, args = sql + " WHERE owner_user_id = ?", (owner_user_id,)
    return [dict(r) for r in conn.execute(sql + " ORDER BY full_name", args)]


@locked
def get_repo(conn, repo_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM repos WHERE id = ?", (repo_id,)).fetchone()
    return dict(row) if row else None


@locked
def repo_by_name(conn, full_name: str) -> dict | None:
    row = conn.execute("SELECT * FROM repos WHERE full_name = ?", (full_name.lower(),)).fetchone()
    return dict(row) if row else None


@locked
def set_repo_limit(conn, repo_id: int, limit: str | None) -> None:
    with conn:
        conn.execute("UPDATE repos SET auto_pay_limit = ? WHERE id = ?", (limit, repo_id))


@locked
def delete_repo(conn, repo_id: int) -> None:
    with conn:
        conn.execute("DELETE FROM repos WHERE id = ?", (repo_id,))


@locked
def bounties_for_user(conn, user_id: int, login: str) -> list[dict]:
    """Bounties on the user's repos, plus the ones they are the contributor on."""
    return [dict(r) for r in conn.execute(
        "SELECT b.*, c.paypal_email FROM bounties b "
        "LEFT JOIN contributors c ON c.github_login = b.contributor "
        "WHERE b.repo IN (SELECT full_name FROM repos WHERE owner_user_id = ?) OR lower(b.contributor) = lower(?) "
        "ORDER BY b.id DESC", (user_id, login))]


@locked
def events_for(conn, bounty_ids: list[int], login: str, limit: int = 100) -> list[dict]:
    marks = ", ".join("?" * len(bounty_ids)) or "NULL"
    return [dict(r) for r in conn.execute(
        f"SELECT * FROM events WHERE bounty_id IN ({marks}) OR (bounty_id IS NULL AND message = ?) "
        "ORDER BY id DESC LIMIT ?", (*bounty_ids, f"PayPal email saved for {login}", limit))]
