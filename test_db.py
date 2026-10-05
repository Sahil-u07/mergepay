import re
import sqlite3
from decimal import Decimal

import pytest

import db


@pytest.fixture
def conn():
    return db.connect(":memory:")


def test_create_and_find_open_bounty(conn):
    bid = db.create_bounty(conn, "Owner/Repo", 7, Decimal("40.00"))
    row = db.open_bounty_for(conn, "owner/repo", 7)
    assert row["id"] == bid and row["amount"] == "40.00" and row["status"] == "open"
    assert "created" in db.events(conn)[0]["message"]


def test_one_bounty_per_issue(conn):
    db.create_bounty(conn, "o/r", 7, Decimal("40"))
    with pytest.raises(Exception):
        db.create_bounty(conn, "o/r", 7, Decimal("10"))


def test_only_one_claim_wins(conn):
    bid = db.create_bounty(conn, "o/r", 7, Decimal("40"))
    assert db.claim(conn, bid) is True
    assert db.claim(conn, bid) is False  # a duplicate webhook delivery loses
    assert db.open_bounty_for(conn, "o/r", 7) is None


def test_update_stores_json(conn):
    bid = db.create_bounty(conn, "o/r", 7, Decimal("40"))
    db.update(conn, bid, status="needs_approval", reasons=["Amount above limit"], pr=12)
    row = db.get_bounty(conn, bid)
    assert row["status"] == "needs_approval" and row["reasons"] == '["Amount above limit"]' and row["pr"] == 12


def test_contributor_lookup_is_case_insensitive_and_updatable(conn):
    db.set_contributor(conn, "Dev123", "old@example.com")
    db.set_contributor(conn, "dev123", "new@example.com")
    assert db.contributor_email(conn, "DEV123") == "new@example.com"
    assert db.contributor_email(conn, "nobody") is None


OLD_SCHEMA = """
CREATE TABLE bounties (id INTEGER PRIMARY KEY, repo TEXT NOT NULL, issue INTEGER NOT NULL, amount TEXT NOT NULL,
    currency TEXT NOT NULL DEFAULT 'USD', status TEXT NOT NULL DEFAULT 'open', pr INTEGER, contributor TEXT,
    verdict TEXT, reasons TEXT, payout_batch_id TEXT, UNIQUE (repo, issue));
CREATE TABLE contributors (github_login TEXT PRIMARY KEY COLLATE NOCASE, paypal_email TEXT NOT NULL);
CREATE TABLE events (id INTEGER PRIMARY KEY, bounty_id INTEGER, at TEXT NOT NULL DEFAULT (datetime('now')), message TEXT NOT NULL);
INSERT INTO bounties (id, repo, issue, amount, status) VALUES (3, 'o/r', 1, '10', 'failed'), (4, 'o/r', 2, '10', 'open');
"""


def test_database_from_before_refs_is_upgraded(tmp_path):
    path = tmp_path / "old.db"
    sqlite3.connect(path).executescript(OLD_SCHEMA)
    conn = db.connect(str(path))
    # The old batch id was "mergepay-<id>". A payout may already have been tried for a failed
    # bounty, so it keeps that id (PayPal then refuses a second payment). Untouched ones get a random ref.
    assert db.get_bounty(conn, 3)["ref"] == "3"
    assert re.fullmatch(r"[0-9a-f]{16}", db.get_bounty(conn, 4)["ref"])
    db.create_bounty(conn, "o/r", 5, Decimal("1"), issue_title="t", issue_body="b")  # new columns exist
    db.connect(str(path))  # upgrading twice is harmless


def test_interrupted_payments_go_to_the_maintainer(conn):
    bid = db.create_bounty(conn, "o/r", 7, Decimal("40"))
    db.claim(conn, bid)  # the process died while the bounty was 'paying'
    assert db.recover_interrupted(conn) == 1
    row = db.get_bounty(conn, bid)
    assert row["status"] == "needs_approval" and "check PayPal" in row["reasons"]


def test_move_only_from_allowed_states(conn):
    bid = db.create_bounty(conn, "o/r", 7, Decimal("40"))
    assert db.move(conn, bid, ("needs_approval",), "rejected") is False
    assert db.get_bounty(conn, bid)["status"] == "open"
    assert db.move(conn, bid, ("open",), "rejected") is True
    assert db.get_bounty(conn, bid)["status"] == "rejected"
