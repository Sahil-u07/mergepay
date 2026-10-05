"""Payment rules. Plain code, so the AI can never talk its way into a payment.

The AI's verdict can only do two things: allow an automatic payment, or send the
bounty to the maintainer. It can never block a payment on its own, because a
maintainer already merged the PR and gets the final say.
"""
import os
from dataclasses import dataclass, field
from decimal import Decimal

AUTO_PAY_LIMIT = Decimal(os.environ.get("AUTO_PAY_LIMIT", "50"))


@dataclass
class Decision:
    action: str  # "pay" or "needs_approval"
    reasons: list[str] = field(default_factory=list)


def decide(amount: Decimal, solves_issue: bool, confidence: str, manipulation_attempt: bool,
           recipient_email: str | None, limit: Decimal = AUTO_PAY_LIMIT) -> Decision:
    reasons = []
    if manipulation_attempt:
        reasons.append("The PR contains text that tries to influence the AI reviewer.")
    if not solves_issue:
        reasons.append("The AI reviewer thinks this PR does not solve the issue.")
    elif confidence != "high":
        reasons.append(f"The AI reviewer's confidence is only {confidence}.")
    if amount > limit:
        reasons.append(f"Amount {amount} is above the auto-pay limit of {limit}.")
    if not recipient_email:
        reasons.append("The contributor has not registered a PayPal email.")
    return Decision("needs_approval", reasons) if reasons else Decision("pay")
