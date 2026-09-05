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

BROKER_URL = os.environ.get("BROKER_URL", "http://172.18.0.1:8610")
APP_ID = os.environ.get("BROKER_APP_ID", "movies")

# The subscription path shells out to the Claude CLI, which is slower than the
# API. A recommendation run is the longest call this app makes.
TIMEOUT_S = int(os.environ.get("BROKER_TIMEOUT", "240"))


class BrokerError(RuntimeError):
    """The broker refused or could not answer. Callers degrade, never crash."""


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
    except requests.RequestException as exc:
        raise BrokerError(f"could not reach the ai-broker: {exc}") from exc

    if resp.status_code != 200:
        # Surface the broker's own reason. "budget_exceeded" and
        # "max_not_permitted" are things a person can act on; a bare 500 is not.
        try:
            body = resp.json()
            detail = body.get("detail") or body.get("error") or resp.text[:200]
        except ValueError:
            detail = resp.text[:200]
        raise BrokerError(f"broker returned {resp.status_code}: {detail}")

    data = resp.json().get("data")
    if not isinstance(data, dict):
        raise BrokerError("broker returned no structured answer")
    return data
