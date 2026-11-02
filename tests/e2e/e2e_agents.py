"""Instrumented agents used by the end-to-end tests (deterministic, offline).

Imported both by the tests and by ``traceon record/replay --entry e2e_agents:...``.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from typing import Any

from traceon import model, now, span, tool, uuid4

REAL_CALLS: Counter[str] = Counter()


@tool
def search(query: str, limit: int = 2) -> list[str]:
    REAL_CALLS["search"] += 1
    return [f"{query}-result-{i}" for i in range(limit)]


@tool
def fetch(doc: str, api_key: str = "sk-proj-e2eSecretValue1234567890abcd") -> dict[str, Any]:
    REAL_CALLS["fetch"] += 1
    return {"doc": doc, "length": len(doc)}


@tool
def flaky(doc: str) -> str:
    REAL_CALLS["flaky"] += 1
    raise ConnectionError(f"upstream reset while reading {doc}")


@tool(side_effect=True)
def send_email(to: str, body: str) -> dict[str, str]:
    REAL_CALLS["send_email"] += 1
    return {"status": "sent", "to": to}


@model(provider="offline")
def answer(question: str, docs: list[dict[str, Any]]) -> dict[str, Any]:
    REAL_CALLS["answer"] += 1
    return {
        "text": f"{question}: {len(docs)} documents",
        "usage": {"input_tokens": 10 * len(docs), "output_tokens": 5},
    }


def research(question: str, limit: int = 2) -> dict[str, Any]:
    """Baseline agent: uuid -> now -> search -> fetch* -> answer -> send_email."""
    rid = uuid4()
    started = now()
    with span("retrieve"):
        hits = search(question, limit=limit)
        docs = [fetch(h) for h in hits]
    with span("respond"):
        result = answer(question, docs)
    send_email("ops@example.com", result["text"])
    return {"id": rid, "at": started.isoformat(), "text": result["text"]}


def research_changed_limit(question: str, limit: int = 2) -> dict[str, Any]:
    """Regression: asks the search tool for one more result."""
    return research(question, limit=limit + 1)


def research_skips_email(question: str, limit: int = 2) -> dict[str, Any]:
    """Regression: stops before the side effect."""
    hits = search(question, limit=limit)
    docs = [fetch(h) for h in hits]
    uuid4()
    return {"text": answer(question, docs)["text"]}


def research_with_failure(question: str) -> str:
    """Calls a tool that fails; the agent handles the error."""
    try:
        return flaky(question)
    except ConnectionError as exc:
        return f"degraded: {exc}"


@tool
async def async_lookup(key: str) -> dict[str, str]:
    REAL_CALLS["async_lookup"] += 1
    await asyncio.sleep(0.001 * (3 - len(key) % 3))
    return {"key": key, "value": key.upper()}


async def research_async(keys: list[str]) -> list[dict[str, str]]:
    """Concurrent tool calls: completion order differs from start order."""
    async with span("fan-out"):
        return list(await asyncio.gather(*(async_lookup(k) for k in keys)))
