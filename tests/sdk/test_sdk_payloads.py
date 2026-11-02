from __future__ import annotations

import json

import pytest

from traceon.core.canonical import canonical_json, content_hash
from traceon.sdk.payloads import TRUNCATED_KEY, build_payload, prepare_value, truncate_value
from traceon.sdk.policy import RedactionPolicy

POLICY = RedactionPolicy()


def test_capture_states_are_distinct() -> None:
    not_captured = build_payload("x", capture=False, redaction=POLICY, max_bytes=1024)
    hashed = build_payload("x", capture="hash", redaction=POLICY, max_bytes=1024)
    captured_none = build_payload(None, capture=True, redaction=POLICY, max_bytes=1024)
    redacted = build_payload("Bearer abcdefghijkl", capture=True, redaction=POLICY, max_bytes=1024)
    whole = build_payload({"password": "x"}, capture=True, redaction=POLICY, max_bytes=1024)

    assert not_captured.state == "not_captured" and not_captured.sha256 is None
    assert hashed.state == "hash_only" and hashed.value is None
    assert hashed.sha256 == content_hash("x")
    assert captured_none.state == "captured" and captured_none.value is None
    assert captured_none.sha256 == content_hash(None)
    # Partially redacted string: still captured, path reported.
    assert redacted.state == "captured" and redacted.redacted_paths == ["$"]
    assert whole.state == "captured" and whole.value == {"password": "[REDACTED:sensitive_key]"}


def test_fully_redacted_value_has_redacted_state() -> None:
    secret = "sk-" + "proj-" + "A1b2C3d4E5f6G7h8I9j0K1l2"
    payload = build_payload(secret, capture=True, redaction=POLICY, max_bytes=1024)
    assert payload.state == "redacted"
    assert payload.value is None
    assert payload.redacted_paths == ["$"]
    assert payload.sha256 == content_hash("[REDACTED:openai_api_key]")


def test_sha256_matches_content_hash_of_redacted_value() -> None:
    value = {"q": "hello", "auth": "secret", "n": [1, 2.5, None]}
    prepared = prepare_value(value, POLICY)
    assert prepared.sha256 == content_hash(prepared.value)
    assert prepared.size_bytes == len(canonical_json(prepared.value).encode())


def test_long_string_truncated_with_marker_and_full_hash() -> None:
    value = "é" * 5000
    payload = build_payload(value, capture=True, redaction=POLICY, max_bytes=300)
    assert payload.truncated is True
    assert payload.size_bytes == len(json.dumps(value, ensure_ascii=False).encode())
    assert payload.sha256 == content_hash(value)
    assert isinstance(payload.value, str)
    assert payload.value.startswith("éé")
    assert payload.value.endswith(f"of {payload.size_bytes - 2} bytes]")
    assert len(canonical_json(payload.value).encode()) <= 300
    assert not payload.is_servable


@pytest.mark.parametrize("value", [list(range(5000)), {f"k{i:04d}": "v" * 20 for i in range(500)}])
def test_large_containers_become_truncation_summaries(value: object) -> None:
    payload = build_payload(value, capture=True, redaction=POLICY, max_bytes=512)
    assert payload.truncated
    summary = payload.value[TRUNCATED_KEY]
    assert summary["type"] in {"array", "object"}
    assert summary["length"] == len(value)  # type: ignore[arg-type]
    assert summary["size_bytes"] == payload.size_bytes
    assert len(summary["preview"]) > 0
    assert len(canonical_json(payload.value).encode()) <= 512
    if isinstance(value, list):
        assert summary["preview"] == value[: len(summary["preview"])]


def test_container_with_one_huge_string_gets_cut_preview() -> None:
    value = {"a": "x" * 10_000, "b": 1}
    out = truncate_value(value, 400)
    preview = out[TRUNCATED_KEY]["preview"]
    assert preview["a"].endswith("bytes]")
    assert len(canonical_json(out).encode()) <= 400


def test_small_values_untouched() -> None:
    assert truncate_value({"a": 1}, 256) == {"a": 1}
    payload = build_payload({"a": 1}, capture=True, redaction=POLICY, max_bytes=256)
    assert payload.truncated is False and payload.value == {"a": 1}


def test_redaction_happens_before_truncation() -> None:
    secret = "ghp" + "_" + "Q" * 36
    value = "prefix " + secret + " " + "y" * 4000
    payload = build_payload(value, capture=True, redaction=POLICY, max_bytes=300)
    assert secret not in json.dumps(payload.model_dump())
    assert payload.redacted_paths == ["$"]


def test_pipeline_does_not_mutate_caller_objects() -> None:
    value = {"password": "hunter2", "items": [{"token": "abc"}]}
    build_payload(value, capture=True, redaction=POLICY, max_bytes=1024)
    assert value == {"password": "hunter2", "items": [{"token": "abc"}]}
