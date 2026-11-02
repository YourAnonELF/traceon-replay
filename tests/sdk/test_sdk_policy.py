from __future__ import annotations

import copy

import pytest
from sdk_testutil import SECRETS

from traceon.core.canonical import canonicalize
from traceon.sdk.policy import (
    DEFAULT_VALUE_PATTERNS,
    KEY_RULE,
    CapturePolicy,
    RedactionPolicy,
    luhn_valid,
)


@pytest.mark.parametrize("rule", sorted(SECRETS))
def test_each_default_value_rule_redacts_its_secret(rule: str) -> None:
    text, secret = SECRETS[rule]
    redacted, fired = RedactionPolicy().redact_text(text)
    assert secret not in redacted
    assert f"[REDACTED:{rule}]" in redacted
    assert rule in fired


def test_every_default_rule_has_a_test_sample() -> None:
    names = {name for name, _ in DEFAULT_VALUE_PATTERNS}
    assert names == set(SECRETS)


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "Password",
        "db_password",
        "passwd",
        "secret",
        "client_secret",
        "token",
        "access_token",
        "accessToken",
        "refresh-token",
        "api_key",
        "apiKey",
        "APIKey",
        "x-api-key",
        "Authorization",
        "cookie",
        "Set-Cookie",
        "session",
        "session_id",
        "sessionId",
        "private_key",
        "privateKey",
        "credentials",
        "auth",
        "csrf_token",
        "dsn",
        "connectionString",
    ],
)
def test_sensitive_keys_are_redacted(key: str) -> None:
    redacted, paths = RedactionPolicy().apply({key: "plain-value", "other": 1})
    assert redacted[key] == f"[REDACTED:{KEY_RULE}]"
    assert redacted["other"] == 1
    assert len(paths) == 1


@pytest.mark.parametrize(
    "key",
    [
        "max_tokens",
        "prompt_tokens",
        "total_tokens",
        "token_count",
        "author",
        "query",
        "sessions_count",
        "passport_no",
    ],
)
def test_benign_keys_are_kept(key: str) -> None:
    assert RedactionPolicy().apply({key: "value"}) == ({key: "value"}, [])


def test_key_redaction_replaces_whole_subtree_and_reports_paths() -> None:
    value = {"config": {"credentials": {"user": "u", "pw": "p"}}, "items": [{"api-key": "k"}]}
    redacted, paths = RedactionPolicy().apply(value)
    assert redacted["config"]["credentials"] == "[REDACTED:sensitive_key]"
    assert redacted["items"][0]["api-key"] == "[REDACTED:sensitive_key]"
    assert sorted(paths) == ["$.config.credentials", '$.items[0]["api-key"]']


def test_null_values_under_sensitive_keys_stay_null() -> None:
    assert RedactionPolicy().apply({"password": None}) == ({"password": None}, [])


def test_apply_never_mutates_input() -> None:
    value = canonicalize({"password": "x", "nested": [{"note": SECRETS["jwt"][0]}]})
    before = copy.deepcopy(value)
    RedactionPolicy().apply(value)
    assert value == before


def test_secret_inside_dict_key_is_redacted() -> None:
    secret_text = SECRETS["github_token"][1]
    redacted, paths = RedactionPolicy().apply({secret_text: 1})
    assert secret_text not in str(redacted)
    assert paths and "REDACTED" in paths[0]


def test_bearer_keeps_scheme_and_only_hides_token() -> None:
    out, _ = RedactionPolicy().redact_text("Authorization: Bearer abcdefghijklmnop")
    assert out == "Authorization: Bearer [REDACTED:bearer_token]"


def test_jwt_inside_bearer_is_redacted_once() -> None:
    text, secret = SECRETS["jwt"]
    out, fired = RedactionPolicy().redact_text("Bearer " + text.split(" ", 1)[1])
    assert secret not in out
    assert fired == ["jwt"]


def test_credit_card_requires_luhn() -> None:
    policy = RedactionPolicy()
    assert policy.redact_text("4111 1111 1111 1112")[1] == []
    assert policy.redact_text("order 1234567890123456")[1] == []
    assert policy.redact_text("4111-1111-1111-1111")[1] == ["credit_card"]
    assert luhn_valid("378282246310005")  # Amex test number
    assert not luhn_valid("12345")


def test_emails_off_by_default_and_optional() -> None:
    assert RedactionPolicy().redact_text("mail alice@example.com")[1] == []
    out, fired = RedactionPolicy(redact_emails=True).redact_text("mail alice@example.com")
    assert out == "mail [REDACTED:email]"
    assert fired == ["email"]
    assert "email" in RedactionPolicy(redact_emails=True).rules


def test_ordinary_text_untouched() -> None:
    text = "The task-runner uses sk-learn style APIs; call me at 555-0100 on 2024-01-01."
    assert RedactionPolicy().redact_text(text) == (text, [])


def test_custom_rules_and_placeholder() -> None:
    policy = RedactionPolicy(placeholder="<{rule}>").with_rules(
        key_patterns=[r"ssn"], value_patterns=[("ticket", r"TCK-\d{6}")], policy_id="acme"
    )
    redacted, paths = policy.apply({"customer_ssn": "123", "note": "see TCK-123456"})
    assert redacted == {"customer_ssn": "<sensitive_key>", "note": "see <ticket>"}
    assert len(paths) == 2
    assert policy.policy_id == "acme"
    assert policy.is_placeholder("<ticket>")


def test_disabled_policy_redacts_nothing() -> None:
    policy = RedactionPolicy.disabled()
    assert policy.apply({"password": "x"}) == ({"password": "x"}, [])
    assert policy.rules == []


def test_invalid_patterns_raise_clear_errors() -> None:
    with pytest.raises(ValueError, match="invalid redaction key pattern"):
        RedactionPolicy(key_patterns=["("])
    with pytest.raises(ValueError, match="invalid redaction value pattern 'bad'"):
        RedactionPolicy(value_patterns=[("bad", "[")])
    with pytest.raises(ValueError, match="placeholder"):
        RedactionPolicy(placeholder="{nope}")


def test_policies_are_immutable_and_hashable() -> None:
    policy = RedactionPolicy(key_patterns=["a"])  # list input is normalized to a tuple
    assert isinstance(policy.key_patterns, tuple)
    assert hash(policy) == hash(RedactionPolicy(key_patterns=("a",)))
    with pytest.raises(AttributeError):
        policy.policy_id = "x"  # type: ignore[misc]


def test_capture_policy_validation() -> None:
    with pytest.raises(ValueError, match="model_prompts"):
        CapturePolicy(model_prompts="yes")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="at least"):
        CapturePolicy(max_payload_bytes=10)
    assert CapturePolicy.full().model_prompts is True
    minimal = CapturePolicy.minimal()
    assert not minimal.tool_args and not minimal.tool_results and not minimal.error_messages
