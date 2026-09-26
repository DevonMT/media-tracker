"""The one place this app talks to Claude.

It does not hold an API key. Every call goes to the ai-broker on the mini,
which decides whether it runs on Devon's Claude Max subscription or on a
metered API key, enforces the monthly budget, and writes an audit line.

Why the indirection is worth it: this app is admin-only, so its calls belong on
the subscription and should cost nothing. Deciding that here would mean this
app knowing about Anthropic's licence terms, and every other app knowing them
too, and all of them drifting. The broker asks the platform who can reach an
app and refuses the subscription path if the answer is "not only admins" — so
the rule lives in one place and is enforced rather than remembered.

The seam is deliberately narrow: one function, structured output only. Tests
mock this rather than the Anthropic SDK.
"""

from __future__ import annotations

import os

import requests

BROKER_URL = os.environ.get("BROKER_URL", "http://172.30.0.1:8610")
APP_ID = os.environ.get("BROKER_APP_ID", "matinee")

# The subscription path shells out to the Claude CLI, which is slower than the
# API. A recommendation run is the longest call this app makes.
TIMEOUT_S = int(os.environ.get("BROKER_TIMEOUT", "240"))


class BrokerError(RuntimeError):
    """The broker refused or could not answer. Callers degrade, never crash.

    str(exc) is the full technical reason, for the server log: it names hosts,
    ports and budgets, so it never goes on a page. `kind` is what a person can
    be told, and `public_message()` is the sentence to tell them:

      unreachable  no connection to the broker at all
      timeout      connected, but no answer inside TIMEOUT_S
      refused      the broker said no on purpose (budget, policy, the model
                   declining) — retrying in a minute will not help
      failed       the broker or the model broke; worth another go
    """

    # Broker codes that are a decision rather than a fault. Anything else it
    # returns is treated as a failure, so a new code degrades to "try again".
    REFUSALS = {
        "budget_exceeded": "Matinee's AI budget is spent for this month, so "
                           "recommendations are paused until it resets.",
        "user_budget_exceeded": "You've used your share of the AI budget this "
                                "month, so recommendations are paused until it resets.",
        "max_not_permitted": "Recommendations are switched off for Matinee right now.",
        "ai_disabled": "Recommendations are switched off for Matinee right now.",
        "unknown_app": "Recommendations are switched off for Matinee right now.",
        "model_not_allowed": "Recommendations are switched off for Matinee right now.",
        "model_refused": "The recommender declined that request. Try a different "
                         "group or setting.",
    }

    def __init__(self, message: str, kind: str = "failed", code: str | None = None):
        super().__init__(message)
        self.kind = kind
        self.code = code

    def public_message(self) -> str:
        if self.kind == "unreachable":
            return "The recommender is unavailable right now. Try again in a minute."
        if self.kind == "timeout":
            return "The recommender took too long to answer. Try again in a minute."
        if self.kind == "refused":
            return self.REFUSALS.get(self.code, "The recommender turned that request down.")
        return "The recommender couldn't come up with picks this time. Try again in a minute."


def ask_structured(prompt: str, schema: dict, max_tokens: int = 4096) -> dict:
    """Ask Claude for an answer shaped by `schema`, and return it parsed.

    The broker guarantees the result is valid JSON matching the requested
    shape, or it raises — so callers never parse prose and never see a
    half-finished tool call.
    """
    try:
        resp = requests.post(
            f"{BROKER_URL}/v1/ask",
            json={
                "app_id": APP_ID,
                "prompt": prompt,
                "schema": schema,
                "max_tokens": max_tokens,
            },
            timeout=TIMEOUT_S,
        )
    except requests.Timeout as exc:
        # Before RequestException: Timeout is a subclass, and "slow" is a
        # different thing to tell someone than "not there".
        raise BrokerError(f"ai-broker timed out after {TIMEOUT_S}s: {exc}",
                          kind="timeout") from exc
    except requests.RequestException as exc:
        raise BrokerError(f"could not reach the ai-broker: {exc}",
                          kind="unreachable") from exc

    if resp.status_code != 200:
        # Keep the broker's own reason. "budget_exceeded" and
        # "max_not_permitted" are things a person can act on; a bare 500 is not.
        # The broker answers {"error": <code>, "detail": <message>}.
        code = None
        try:
            body = resp.json()
            code = body.get("error")
            detail = body.get("detail") or code or resp.text[:200]
        except (ValueError, AttributeError):
            detail = resp.text[:200]
        kind = "refused" if code in BrokerError.REFUSALS else "failed"
        raise BrokerError(f"broker returned {resp.status_code} [{code}]: {detail}",
                          kind=kind, code=code)

    try:
        data = resp.json().get("data")
    except (ValueError, AttributeError):
        data = None
    if not isinstance(data, dict):
        raise BrokerError("broker returned no structured answer")
    return data
