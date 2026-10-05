from decimal import Decimal

import pytest

from policy import decide

GOOD = dict(amount=Decimal("40"), solves_issue=True, confidence="high", manipulation_attempt=False,
            recipient_email="dev@example.com", limit=Decimal("50"))


def test_clean_small_bounty_is_paid():
    assert decide(**GOOD).action == "pay"


def test_limit_is_inclusive():
    assert decide(**{**GOOD, "amount": Decimal("50")}).action == "pay"


@pytest.mark.parametrize("change, reason", [
    ({"amount": Decimal("50.01")}, "auto-pay limit"),
    ({"confidence": "medium"}, "confidence"),
    ({"solves_issue": False}, "does not solve"),
    ({"manipulation_attempt": True}, "influence the AI"),
    ({"recipient_email": None}, "PayPal email"),
])
def test_anything_doubtful_goes_to_the_maintainer(change, reason):
    d = decide(**{**GOOD, **change})
    assert d.action == "needs_approval"
    assert any(reason in r for r in d.reasons), d.reasons


def test_manipulation_is_flagged_even_with_a_confident_yes():
    d = decide(**{**GOOD, "manipulation_attempt": True})
    assert d.action == "needs_approval"

