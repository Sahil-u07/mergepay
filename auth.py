"""Sign in with GitHub (an OAuth App) and server-side sessions.

MergePay asks GitHub for no scopes: it only learns who you are and can read public repos
with your token, which is how it checks that you maintain a repo before you connect it.
"""
import hashlib
import secrets
from urllib.parse import urlencode

import httpx

SESSION_COOKIE = "mp_session"
STATE_COOKIE = "mp_oauth_state"
SESSION_SECONDS = 30 * 24 * 3600


class OAuthError(Exception):
    pass


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    """Only this hash is stored, so a leaked database can't be used to sign in."""
    return hashlib.sha256(token.encode()).hexdigest()


class GitHubOAuth:
    def __init__(self, client_id: str, client_secret: str, http: httpx.Client | None = None):
        self.client_id, self.client_secret = client_id, client_secret
        self.http = http or httpx.Client(timeout=15)

    def authorize_url(self, redirect_uri: str, state: str) -> str:
        return "https://github.com/login/oauth/authorize?" + urlencode(
            {"client_id": self.client_id, "redirect_uri": redirect_uri, "state": state, "allow_signup": "true"})

    def exchange(self, code: str, redirect_uri: str) -> str:
        r = self.http.post("https://github.com/login/oauth/access_token", headers={"Accept": "application/json"},
                           data={"client_id": self.client_id, "client_secret": self.client_secret,
                                 "code": code, "redirect_uri": redirect_uri})
        r.raise_for_status()
        data = r.json()
        if "access_token" not in data:  # GitHub answers 200 with an "error" field for bad codes
            raise OAuthError(data.get("error", "no access token"))
        return data["access_token"]

    def _get(self, token: str, path: str) -> httpx.Response:
        return self.http.get("https://api.github.com" + path, headers={
            "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"})

    def user(self, token: str) -> dict:
        r = self._get(token, "/user")
        r.raise_for_status()
        return r.json()

    def repo(self, token: str, full_name: str) -> dict | None:
        """The repo as this user sees it, including their `permissions`; None if they can't see it."""
        r = self._get(token, f"/repos/{full_name}")
        if r.status_code == 401:  # the user revoked MergePay on GitHub
            raise OAuthError("GitHub token no longer valid")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()
