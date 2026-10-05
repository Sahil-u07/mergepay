import hashlib
import hmac

import httpx
import pytest

from github import GitHub, closing_issues, verify_signature

SECRET = "s3cret"
BODY = b'{"action":"closed"}'
GOOD = "sha256=" + hmac.new(SECRET.encode(), BODY, hashlib.sha256).hexdigest()


def test_valid_signature_passes():
    assert verify_signature(SECRET, BODY, GOOD)


@pytest.mark.parametrize("header", [
    None,                                   # header missing
    "",                                     # header empty
    GOOD.replace("sha256=", "sha1="),       # wrong algorithm prefix
    GOOD[:-1] + ("0" if GOOD[-1] != "0" else "1"),  # one character changed
    "sha256=" + hmac.new(b"wrong", BODY, hashlib.sha256).hexdigest(),  # wrong secret
])
def test_bad_signatures_fail(header):
    assert not verify_signature(SECRET, BODY, header)


def test_body_change_fails():
    assert not verify_signature(SECRET, BODY + b" ", GOOD)


@pytest.mark.parametrize("body, expected", [
    ("Fixes #7", [7]),
    ("fixes #7", [7]),
    ("This PR closes #12 and resolves #3.", [12, 3]),
    ("Resolved: #5", [5]),
    ("Fixed #7, fixed #7 again", [7]),          # no duplicates
    ("Related to #7", []),                       # not a closing keyword
    ("prefixes #7", []),                         # keyword must be a whole word
    ("Fixes other/repo#9", []),                  # other repos don't count
    ("", []),
    (None, []),
])
def test_closing_issues(body, expected):
    assert closing_issues(body) == expected


def fake_github(routes):
    seen = []

    def handler(request):
        seen.append(request)
        return routes[request.url.path](request)
    return GitHub("tok", http=httpx.Client(base_url="https://api.github.com",
                                           transport=httpx.MockTransport(handler))), seen


def test_issue_fetches_title_and_body():
    gh, seen = fake_github({"/repos/o/r/issues/7": lambda r: httpx.Response(
        200, json={"title": "Crash on empty name", "body": None})})
    assert gh.issue("o/r", 7) == {"title": "Crash on empty name", "body": ""}


def test_pr_diff_asks_for_diff_media_type():
    gh, seen = fake_github({"/repos/o/r/pulls/12": lambda r: httpx.Response(200, text="diff --git a/x b/x")})
    assert gh.pr_diff("o/r", 12).startswith("diff --git")
    assert seen[0].headers["Accept"] == "application/vnd.github.diff"
