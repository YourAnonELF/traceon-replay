"""An order-status agent whose final step contains a parser bug.

Flow for ``run_agent("A-1042")``:

1. ``lookup_order`` (tool) returns the carrier and tracking number,
2. ``track_shipment`` (tool) returns the carrier's raw status line, e.g.
   ``"IN_TRANSIT;ETA=2026-11-05T14:00:00+01:00;LOC=Leipzig"``,
3. ``draft_reply`` (model) writes the customer-facing message,
4. the agent extracts a machine-readable ETA for the UI.

Version 1 (``run_agent_v1``) parses the ETA with a ``strptime`` format that has
no UTC offset, so it fails on the carrier's real data with
``ValueError: unconverted data remains: +01:00``. ``run_agent`` is the fixed
version. ``run_agent_refactored`` is the fixed version plus an unrelated
"cleanup" that lowercases order ids -- a behavior change replay catches.

All tools and the model are deterministic local stand-ins: no network, no keys.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from collections.abc import Callable
from typing import Any

from traceon import model, span, tool

REAL_CALLS: Counter[str] = Counter()

_ORDERS = {"A-1042": {"carrier": "parcelnet", "tracking_number": "PN-88341207"}}
_TRACKING = {"PN-88341207": "IN_TRANSIT;ETA=2026-11-05T14:00:00+01:00;LOC=Leipzig"}


@tool
def lookup_order(order_id: str) -> dict[str, str]:
    """Find carrier and tracking number for an order."""
    REAL_CALLS["lookup_order"] += 1
    try:
        return {"order_id": order_id, **_ORDERS[order_id]}
    except KeyError:
        raise LookupError(f"unknown order {order_id!r}") from None


@tool
def track_shipment(carrier: str, tracking_number: str) -> str:
    """Return the carrier's raw status line."""
    REAL_CALLS["track_shipment"] += 1
    return _TRACKING[tracking_number]


@model(provider="offline-demo")
def draft_reply(order_id: str, status_line: str) -> str:
    """Stand-in for an LLM that turns the raw status into a customer message."""
    REAL_CALLS["draft_reply"] += 1
    fields = dict(part.split("=", 1) for part in status_line.split(";")[1:])
    return f"Order {order_id} is on its way (last seen in {fields['LOC']}) and should arrive soon."


def parse_eta_v1(status_line: str) -> dt.datetime:
    """BUG: the format string ignores the UTC offset the carrier sends."""
    raw = status_line.split("ETA=", 1)[1].split(";", 1)[0]
    return dt.datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S")


def parse_eta(status_line: str) -> dt.datetime:
    """Fixed: ISO 8601 with offset, normalized to UTC."""
    raw = status_line.split("ETA=", 1)[1].split(";", 1)[0]
    return dt.datetime.fromisoformat(raw).astimezone(dt.timezone.utc)


def _run(order_id: str, parse: Callable[[str], dt.datetime]) -> dict[str, Any]:
    with span("gather"):
        order = lookup_order(order_id)
        status_line = track_shipment(order["carrier"], order["tracking_number"])
    with span("respond"):
        reply = draft_reply(order_id, status_line)
        eta = parse(status_line)
    return {"order_id": order_id, "reply": reply, "eta_utc": eta.isoformat()}


def run_agent_v1(order_id: str) -> dict[str, Any]:
    """The version that failed in production."""
    return _run(order_id, parse_eta_v1)


def run_agent(order_id: str) -> dict[str, Any]:
    """The fixed version."""
    return _run(order_id, parse_eta)


def run_agent_refactored(order_id: str) -> dict[str, Any]:
    """Fixed parser plus a refactor that changes the tool argument."""
    return _run(order_id.lower(), parse_eta)
