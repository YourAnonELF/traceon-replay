"""A small research agent with deterministic, offline stand-ins for its tools and model.

The agent answers a question in three steps:

1. ``search`` finds candidate documents (a tool),
2. ``fetch_document`` loads the most relevant ones (a tool),
3. ``summarize`` writes the answer (a model call).

The "tools" and the "model" below are local functions so the example needs no
network and no API keys. In a real agent they would call a search API, a document
store and an LLM provider -- the instrumentation would be exactly the same.

``REAL_CALLS`` counts how often the real implementations run. During replay it
stays at zero: every result is served from the recorded fixture.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from traceon import model, now, span, tool, uuid4

REAL_CALLS: Counter[str] = Counter()

_CORPUS: dict[str, dict[str, str]] = {
    "doc-replay": {
        "title": "Record and replay testing",
        "text": (
            "Record-and-replay testing captures the responses of external systems once "
            "and serves them from a cassette in later runs, which makes tests fast and "
            "deterministic."
        ),
    },
    "doc-agents": {
        "title": "Why agent failures are hard to reproduce",
        "text": (
            "Agent runs depend on model sampling, tool results and wall-clock time, so a "
            "failure seen once in production is often impossible to reproduce locally."
        ),
    },
    "doc-otel": {
        "title": "Tracing with OpenTelemetry",
        "text": "Traces describe what happened in a distributed system as a tree of spans.",
    },
}


@tool
def search(query: str, limit: int = 2) -> list[dict[str, Any]]:
    """Rank documents by how many query words appear in their title and text."""
    REAL_CALLS["search"] += 1
    words = {w.strip("?.,").lower() for w in query.split() if len(w) > 3}
    scored = []
    for doc_id, doc in sorted(_CORPUS.items()):
        haystack = (doc["title"] + " " + doc["text"]).lower()
        score = sum(1 for w in words if w in haystack)
        if score:
            scored.append({"id": doc_id, "title": doc["title"], "score": score})
    scored.sort(key=lambda d: (-d["score"], d["id"]))
    return scored[:limit]


@tool
def fetch_document(doc_id: str) -> dict[str, str]:
    """Load one document by id."""
    REAL_CALLS["fetch_document"] += 1
    doc = _CORPUS[doc_id]
    return {"id": doc_id, "title": doc["title"], "text": doc["text"]}


@model(provider="offline-demo")
def summarize(question: str, documents: list[dict[str, str]]) -> dict[str, Any]:
    """Stand-in for an LLM call: deterministic extractive summary plus token usage."""
    REAL_CALLS["summarize"] += 1
    first_sentences = [d["text"].split(". ")[0].rstrip(".") + "." for d in documents]
    answer = " ".join(first_sentences)
    prompt_tokens = sum(len(d["text"].split()) for d in documents) + len(question.split())
    return {
        "answer": answer,
        "sources": [d["id"] for d in documents],
        "usage": {"input_tokens": prompt_tokens, "output_tokens": len(answer.split())},
    }


def run_agent(question: str) -> dict[str, Any]:
    """Answer ``question`` from the local corpus."""
    request_id = uuid4()  # recorded, so replay sees the same id
    started = now()  # recorded, so replay sees the same timestamp
    with span("retrieve", question=question):
        hits = search(question)
        documents = [fetch_document(hit["id"]) for hit in hits]
    with span("answer"):
        result = summarize(question, documents)
    return {
        "request_id": request_id,
        "answered_at": started.isoformat(),
        "answer": result["answer"],
        "sources": result["sources"],
    }
