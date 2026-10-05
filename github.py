"""GitHub helpers: trust the webhook, then find which issue a PR closes."""
import hashlib
import hmac
import re

import httpx


def verify_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    """Return True only if the X-Hub-Signature-256 header matches the raw request body.

    GitHub sends "sha256=" + hex(HMAC-SHA256(secret, body)).
    Docs: https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries
    """
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    # compare_digest takes the same time however many characters match, so an attacker
    # can't guess the signature one character at a time by measuring response times.
    return hmac.compare_digest(expected, signature_header)


CLOSING = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b:?\s+#(\d+)\b", re.IGNORECASE)


def closing_issues(pr_body: str | None) -> list[int]:
    """Issue numbers this PR closes in the same repo, e.g. "Fixes #7" -> [7].

    GitHub's keywords: close, closes, closed, fix, fixes, fixed, resolve, resolves, resolved.
    Docs: https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/linking-a-pull-request-to-an-issue
    """
    # dict.fromkeys drops duplicates but keeps the order they appeared in.
    return list(dict.fromkeys(int(n) for n in CLOSING.findall(pr_body or "")))


class GitHub:
    """Tiny GitHub REST client: just what the reviewer needs."""

    def __init__(self, token: str, http: httpx.Client | None = None):
        self.http = http or httpx.Client(base_url="https://api.github.com", timeout=30, headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })

    def issue(self, repo: str, number: int) -> dict:
        r = self.http.get(f"/repos/{repo}/issues/{number}")
        r.raise_for_status()
        body = r.json()
        return {"title": body["title"], "body": body.get("body") or ""}

    def pr_diff(self, repo: str, number: int) -> str:
        # Asking for the diff media type returns the unified diff as plain text.
        r = self.http.get(f"/repos/{repo}/pulls/{number}", headers={"Accept": "application/vnd.github.diff"})
        r.raise_for_status()
        return r.text
