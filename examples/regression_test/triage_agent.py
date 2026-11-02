"""A support-ticket triage agent used by the pytest regression example.

``triage("T-311")`` reads the ticket, asks a model to classify it and assigns it
to a queue. ``assign_ticket`` is marked ``side_effect=True``: in production it
would write to the helpdesk. During replay it is never executed.

Tools and model are deterministic local stand-ins (no network, no API keys).
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from traceon import model, span, tool

REAL_CALLS: Counter[str] = Counter()

_TICKETS = {
    "T-311": {
        "subject": "Charged twice for my October invoice",
        "body": "Hi, my card was charged two times for invoice INV-2026-10-118. Please refund one.",
        "customer_tier": "pro",
    }
}


@tool
def fetch_ticket(ticket_id: str) -> dict[str, str]:
    """Load a ticket from the helpdesk."""
    REAL_CALLS["fetch_ticket"] += 1
    return {"id": ticket_id, **_TICKETS[ticket_id]}


@model(provider="offline-demo")
def classify(subject: str, body: str) -> dict[str, Any]:
    """Stand-in for an LLM classifier."""
    REAL_CALLS["classify"] += 1
    text = f"{subject} {body}".lower()
    if "refund" in text or "charged" in text:
        return {"queue": "billing", "confidence": 0.93}
    return {"queue": "general", "confidence": 0.51}


@tool(side_effect=True)
def assign_ticket(ticket_id: str, queue: str, priority: str) -> dict[str, str]:
    """Assign the ticket in the helpdesk (a write in production)."""
    REAL_CALLS["assign_ticket"] += 1
    return {"ticket_id": ticket_id, "queue": queue, "priority": priority, "status": "assigned"}


def triage(ticket_id: str) -> dict[str, str]:
    """Classify a ticket and route it."""
    with span("triage", ticket_id=ticket_id):
        ticket = fetch_ticket(ticket_id)
        label = classify(ticket["subject"], ticket["body"])
        priority = (
            "high" if ticket["customer_tier"] == "pro" and label["queue"] == "billing" else "normal"
        )
        return assign_ticket(ticket_id, label["queue"], priority)
