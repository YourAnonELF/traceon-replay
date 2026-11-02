"""Capture and redaction policies for recording.

Traceon is local-first and safe by default: fixtures are meant to be committed to
repositories and attached to bug reports, so secrets must never reach them.

* :class:`CapturePolicy` decides *which* payloads are stored at all (and whether
  model prompts are stored in full or only as a hash).
* :class:`RedactionPolicy` scrubs whatever *is* stored: values under sensitive
  dictionary keys (``password``, ``api_key``, ``authorization`` ...) and secrets
  recognised by their shape anywhere in a string (provider API keys, JWTs, bearer
  tokens, PEM private keys, credit-card numbers that pass the Luhn check ...).

Redaction always works on *canonical JSON* (the output of
:func:`traceon.core.canonical.canonicalize`) and never mutates its input.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Final, Literal, TypeAlias

from traceon.core.canonical import JSONValue
from traceon.core.limits import DEFAULT_LIMITS

__all__ = [
    "DEFAULT_KEY_PATTERNS",
    "DEFAULT_VALUE_PATTERNS",
    "EMAIL_RULE",
    "KEY_RULE",
    "MIN_PAYLOAD_BYTES",
    "CaptureMode",
    "CapturePolicy",
    "RedactionPolicy",
    "json_path_key",
    "luhn_valid",
]

#: ``True`` = store the payload, ``False`` = do not store it, ``"hash"`` = store only its hash.
CaptureMode: TypeAlias = bool | Literal["hash"]

#: Smallest accepted ``CapturePolicy.max_payload_bytes`` (truncation summaries need room).
MIN_PAYLOAD_BYTES: Final = 256


@dataclass(frozen=True)
class CapturePolicy:
    """Which payloads a recording stores.

    The defaults are local-first and conservative: tool arguments and results are
    stored (replay needs them), model *results* are stored, but model *prompts* are
    stored only as a hash -- enough to detect that a prompt changed during replay
    without keeping its content. Stack traces are off.

    Per-call flags on :func:`traceon.sdk.tool` / :func:`traceon.sdk.model`
    (``capture_args``, ``capture_prompt``, ``capture_result``) take precedence over
    this policy when they are not ``None``.

    Attributes:
        tool_args: Store tool-call arguments.
        tool_results: Store tool-call results.
        model_prompts: Store model requests: ``True``, ``False`` or ``"hash"``.
        model_results: Store model responses.
        run_input: Store the run-level input passed to ``record_run``.
        run_output: Store the run-level output set with ``RunHandle.set_output``.
        error_messages: Store (redacted) exception messages. When ``False`` only the
            exception type is kept.
        stack_traces: Store (redacted) formatted stack traces of failed calls.
        max_payload_bytes: Payloads whose canonical JSON exceeds this size are
            truncated and marked ``truncated=True``.
    """

    tool_args: bool = True
    tool_results: bool = True
    model_prompts: bool | Literal["hash"] = "hash"
    model_results: bool = True
    run_input: bool = True
    run_output: bool = True
    error_messages: bool = True
    stack_traces: bool = False
    max_payload_bytes: int = DEFAULT_LIMITS.max_payload_bytes

    def __post_init__(self) -> None:
        if self.model_prompts not in (True, False, "hash"):
            raise ValueError(
                f"CapturePolicy.model_prompts must be True, False or 'hash', got {self.model_prompts!r}"
            )
        if not isinstance(self.max_payload_bytes, int) or isinstance(self.max_payload_bytes, bool):
            raise TypeError("CapturePolicy.max_payload_bytes must be an int")
        if self.max_payload_bytes < MIN_PAYLOAD_BYTES:
            raise ValueError(
                f"CapturePolicy.max_payload_bytes must be at least {MIN_PAYLOAD_BYTES}, "
                f"got {self.max_payload_bytes}"
            )
        if self.max_payload_bytes > DEFAULT_LIMITS.max_fixture_bytes:
            raise ValueError(
                "CapturePolicy.max_payload_bytes cannot exceed the fixture size limit "
                f"({DEFAULT_LIMITS.max_fixture_bytes} bytes)"
            )

    @classmethod
    def full(cls) -> CapturePolicy:
        """Store every payload in full, including model prompts (still redacted)."""
        return cls(model_prompts=True)

    @classmethod
    def minimal(cls) -> CapturePolicy:
        """Store structure, timings, hashes of model prompts and error types only.

        Fixtures recorded this way are useful for inspection and diffing but cannot
        be replayed, because no responses are stored.
        """
        return cls(
            tool_args=False,
            tool_results=False,
            model_prompts="hash",
            model_results=False,
            run_input=False,
            run_output=False,
            error_messages=False,
        )


# --------------------------------------------------------------------------- redaction

#: Rule name used for values redacted because of their dictionary key.
KEY_RULE: Final = "sensitive_key"

#: Regexes matched (``re.search``, case-insensitive) against *normalized* dict keys:
#: camelCase is split into snake_case and ``-``/``.``/spaces become ``_`` first, so
#: ``apiKey``, ``api-key`` and ``API_KEY`` all normalize to ``api_key``.
DEFAULT_KEY_PATTERNS: Final[tuple[str, ...]] = (
    r"password|passwd|passphrase",
    r"(^|_)pwd($|_)",
    r"secret",
    r"(^|_)token$",
    r"api_?key",
    r"access_?key",
    r"private_?key",
    r"signing_?key|encryption_?key|master_?key",
    r"authorization",
    r"(^|_)auth$",
    r"cookie",
    r"(^|_)session(_?id|_?key|_?token)?$",
    r"credential",
    r"(^|_)(csrf|xsrf)",
    r"(^|_)dsn$",
    r"connection_?string",
)

#: ``(rule_name, regex)`` pairs applied, in order, to every string. When a regex has
#: a named group ``secret`` only that group is replaced (``Bearer [REDACTED:...]``);
#: otherwise the whole match is. Order matters: more specific rules come first.
DEFAULT_VALUE_PATTERNS: Final[tuple[tuple[str, str], ...]] = (
    (
        "pem_private_key",
        r"-----BEGIN[A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?(?:-----END[A-Z0-9 ]*PRIVATE KEY-----|\Z)",
    ),
    ("jwt", r"\beyJ[A-Za-z0-9_-]{5,}\.eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*"),
    ("anthropic_api_key", r"\bsk-ant-[A-Za-z0-9_-]{16,}"),
    (
        "openai_api_key",
        r"\bsk-(?:proj-|svcacct-|admin-|None-)?(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{20,}",
    ),
    ("github_token", r"\b(?:gh[pousr]_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9_]{22,255})\b"),
    ("slack_token", r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),
    ("slack_webhook", r"https://hooks\.slack\.com/(?:services|workflows|triggers)/[A-Za-z0-9/_-]+"),
    ("stripe_key", r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}"),
    ("google_api_key", r"\bAIza[0-9A-Za-z_-]{35}"),
    (
        "aws_access_key_id",
        r"\b(?:AKIA|ASIA|ABIA|ACCA|AGPA|AIDA|AIPA|ANPA|ANVA|AROA|APKA)[A-Z0-9]{16}\b",
    ),
    (
        "aws_secret_access_key",
        r"(?i)aws.{0,20}?secret.{0,20}?[\"']?\s*[:=]\s*[\"']?(?P<secret>[A-Za-z0-9/+]{40})(?![A-Za-z0-9/+])",
    ),
    ("bearer_token", r"(?i)\bbearer\s+(?P<secret>[A-Za-z0-9\-._~+/]{8,}=*)"),
    ("url_credentials", r"(?i)\b[a-z][a-z0-9+.-]*://[^/\s:@\"']+:(?P<secret>[^/\s@\"']+)@"),
    (
        "secret_assignment",
        r"(?i)(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token"
        r"|refresh[_-]?token|client[_-]?secret)[\"']?\s*[:=]\s*[\"']?"
        r"(?P<secret>(?!\[)[^\s\"'&,;}]{3,})",
    ),
    ("credit_card", r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)"),
)

#: Optional rule, enabled with ``RedactionPolicy(redact_emails=True)``.
EMAIL_RULE: Final[tuple[str, str]] = (
    "email",
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b",
)

_CARD_PREFIX = re.compile(r"^(?:4|5[1-5]|2[2-7]|3[47]|3[068]|35|6011|65|64[4-9]|62)")


def luhn_valid(text: str) -> bool:
    """Return whether the digits in ``text`` form a plausible payment-card number.

    Checks length (13-19 digits), a known issuer prefix and the Luhn checksum, which
    keeps false positives on order numbers and timestamps low.
    """
    digits = [c for c in text if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    if not _CARD_PREFIX.match("".join(digits)):
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


#: Extra validation applied to matches of the rule with the given name.
_VALIDATORS: Final[Mapping[str, Callable[[str], bool]]] = {"credit_card": luhn_valid}

_CAMEL_ACRONYM = re.compile(r"([A-Z]+)([A-Z][a-z])")
_CAMEL = re.compile(r"([a-z0-9])([A-Z])")
_SEPARATORS = re.compile(r"[\s\-.]+")


def _normalize_key(key: str) -> str:
    key = _CAMEL_ACRONYM.sub(r"\1_\2", key)
    key = _CAMEL.sub(r"\1_\2", key)
    return _SEPARATORS.sub("_", key).lower()


def json_path_key(path: str, key: str) -> str:
    """Append a dictionary key to a JSONPath (same notation as :mod:`traceon.core.compare`)."""
    if key.isidentifier():
        return f"{path}.{key}"
    escaped = key.replace("\\", "\\\\").replace('"', '\\"')
    return f'{path}["{escaped}"]'


@dataclass(frozen=True)
class _ValueRule:
    name: str
    regex: re.Pattern[str]
    validator: Callable[[str], bool] | None


@dataclass(frozen=True)
class RedactionPolicy:
    """Rules that remove secrets from every stored payload.

    Two kinds of rules run on canonical JSON values:

    * **key rules** -- if a normalized dictionary key matches any of
      ``key_patterns``, its (non-null) value is replaced wholesale by
      ``placeholder.format(rule="sensitive_key")``;
    * **value rules** -- every string (and dictionary key) is scanned with
      ``value_patterns``; matches are replaced by ``placeholder.format(rule=name)``.

    The defaults cover OpenAI, Anthropic, AWS, GitHub, Slack, Stripe and Google API
    keys, JWTs, bearer tokens, credentials in URLs, ``password=...`` style
    assignments, PEM private keys and credit-card numbers (Luhn-checked). Email
    addresses are left alone unless ``redact_emails=True``.

    Extend the defaults instead of replacing them::

        policy = RedactionPolicy().with_rules(
            key_patterns=[r"customer_ssn"],
            value_patterns=[("internal_token", r"\\bitk_[a-z0-9]{24}\\b")],
        )

    Attributes:
        policy_id: Identifier stored in ``Fixture.redaction.policy_id``.
        key_patterns: Regexes searched in normalized dictionary keys (case-insensitive).
        value_patterns: ``(rule_name, regex)`` pairs applied to strings, in order.
        placeholder: Replacement template; ``{rule}`` is the rule name.
        redact_emails: Also redact email addresses.
    """

    policy_id: str = "default"
    key_patterns: Sequence[str] = DEFAULT_KEY_PATTERNS
    value_patterns: Sequence[tuple[str, str]] = DEFAULT_VALUE_PATTERNS
    placeholder: str = "[REDACTED:{rule}]"
    redact_emails: bool = False
    _key_regexes: tuple[re.Pattern[str], ...] = field(
        init=False, repr=False, compare=False, hash=False, default=()
    )
    _value_rules: tuple[_ValueRule, ...] = field(
        init=False, repr=False, compare=False, hash=False, default=()
    )
    _placeholder_re: re.Pattern[str] | None = field(
        init=False, repr=False, compare=False, hash=False, default=None
    )

    def __post_init__(self) -> None:
        if not self.policy_id:
            raise ValueError("RedactionPolicy.policy_id must be a non-empty string")
        if isinstance(self.key_patterns, str) or isinstance(self.value_patterns, str):
            raise TypeError("key_patterns/value_patterns must be sequences, not a single string")
        key_patterns = tuple(self.key_patterns)
        value_patterns = tuple((str(n), str(p)) for n, p in self.value_patterns)
        object.__setattr__(self, "key_patterns", key_patterns)
        object.__setattr__(self, "value_patterns", value_patterns)

        try:
            self.placeholder.format(rule="x")
        except (KeyError, IndexError, ValueError) as exc:
            raise ValueError(
                f"RedactionPolicy.placeholder {self.placeholder!r} is not a valid template; "
                "use '{rule}' as the only field"
            ) from exc

        key_regexes: list[re.Pattern[str]] = []
        for pattern in key_patterns:
            try:
                key_regexes.append(re.compile(pattern, re.IGNORECASE))
            except re.error as exc:
                raise ValueError(f"invalid redaction key pattern {pattern!r}: {exc}") from exc

        rules: list[_ValueRule] = []
        all_values = (*value_patterns, EMAIL_RULE) if self.redact_emails else value_patterns
        for name, pattern in all_values:
            if not name:
                raise ValueError("redaction value rule names must be non-empty")
            try:
                regex = re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"invalid redaction value pattern {name!r}: {exc}") from exc
            rules.append(_ValueRule(name, regex, _VALIDATORS.get(name)))

        parts = self.placeholder.split("{rule}")
        ph_re = re.compile(r"[A-Za-z0-9_:.\-]+".join(re.escape(p) for p in parts))
        object.__setattr__(self, "_key_regexes", tuple(key_regexes))
        object.__setattr__(self, "_value_rules", tuple(rules))
        object.__setattr__(self, "_placeholder_re", ph_re)

    # ------------------------------------------------------------------ factories
    @classmethod
    def disabled(cls) -> RedactionPolicy:
        """A policy that redacts nothing. Only use it for data you know is safe."""
        return cls(policy_id="none", key_patterns=(), value_patterns=())

    def with_rules(
        self,
        *,
        key_patterns: Iterable[str] = (),
        value_patterns: Iterable[tuple[str, str]] = (),
        policy_id: str | None = None,
    ) -> RedactionPolicy:
        """Return a copy with extra key and value rules appended to the current ones."""
        return replace(
            self,
            policy_id=policy_id if policy_id is not None else self.policy_id,
            key_patterns=(*self.key_patterns, *key_patterns),
            value_patterns=(*self.value_patterns, *value_patterns),
        )

    # ------------------------------------------------------------------ introspection
    @property
    def rules(self) -> list[str]:
        """Names of the active rules (stored in ``Fixture.redaction.rules``)."""
        names = [KEY_RULE] if self._key_regexes else []
        names.extend(rule.name for rule in self._value_rules)
        return names

    def key_is_sensitive(self, key: str) -> bool:
        """Return whether values under dictionary key ``key`` are redacted."""
        if not self._key_regexes:
            return False
        normalized = _normalize_key(key)
        return any(r.search(normalized) for r in self._key_regexes)

    def is_placeholder(self, value: Any) -> bool:
        """Return whether ``value`` is exactly one redaction placeholder."""
        return (
            isinstance(value, str)
            and self._placeholder_re is not None
            and self._placeholder_re.fullmatch(value) is not None
        )

    # ------------------------------------------------------------------ redaction
    def redact_text(self, text: str) -> tuple[str, list[str]]:
        """Apply the value rules to ``text``; return the result and the rule names that fired."""
        fired: list[str] = []
        for rule in self._value_rules:
            if rule.regex.search(text) is None:
                continue
            replacement = self.placeholder.format(rule=rule.name)
            hit = False

            def _sub(
                m: re.Match[str], rule: _ValueRule = rule, replacement: str = replacement
            ) -> str:
                nonlocal hit
                whole = m.group(0)
                if rule.validator is not None and not rule.validator(whole):
                    return whole
                hit = True
                if "secret" in rule.regex.groupindex and m.group("secret") is not None:
                    start, end = m.span("secret")
                    base = m.start()
                    return whole[: start - base] + replacement + whole[end - base :]
                return replacement

            text = rule.regex.sub(_sub, text)
            if hit:
                fired.append(rule.name)
        return text, fired

    def apply(self, value: JSONValue) -> tuple[JSONValue, list[str]]:
        """Redact a canonical JSON value.

        Returns a redacted *copy* of ``value`` and the JSONPaths (``$``, ``$.a.b``,
        ``$.items[2]``) of every location that was replaced. ``value`` itself is never
        modified.
        """
        paths: list[str] = []
        return self._walk(value, "$", paths), paths

    def _walk(self, value: JSONValue, path: str, paths: list[str]) -> JSONValue:
        if isinstance(value, str):
            redacted, fired = self.redact_text(value)
            if fired:
                paths.append(path)
            return redacted
        if isinstance(value, Mapping):
            out: dict[str, JSONValue] = {}
            for raw_key, item in value.items():
                key = str(raw_key)
                new_key, key_fired = self.redact_text(key) if self._value_rules else (key, [])
                if key_fired:
                    suffix = 2
                    base_key = new_key
                    while new_key in out or new_key in value:
                        new_key = f"{base_key}#{suffix}"
                        suffix += 1
                sub = json_path_key(path, new_key)
                if key_fired:
                    paths.append(sub)
                if item is not None and self.key_is_sensitive(key):
                    out[new_key] = self.placeholder.format(rule=KEY_RULE)
                    if not key_fired:
                        paths.append(sub)
                    continue
                out[new_key] = self._walk(item, sub, paths)
            return out
        if isinstance(value, (list, tuple)):
            return [self._walk(item, f"{path}[{i}]", paths) for i, item in enumerate(value)]
        return value
