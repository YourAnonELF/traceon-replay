"""Shared helpers for SDK tests (imported by module name, not a conftest).

Secret-shaped test strings are assembled at runtime so that no realistic-looking
credential literal appears in the source tree (keeps secret scanners quiet).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any, ClassVar, Literal, TypeVar

from traceon.core.runtime import CallSpec, Session
from traceon.core.schema import Fixture

T = TypeVar("T")


def _j(*parts: str) -> str:
    return "".join(parts)


#: rule name -> (full text containing the secret, the secret substring that must vanish)
SECRETS: dict[str, tuple[str, str]] = {
    "openai_api_key": (
        _j("key is ", "sk-", "proj-", "Ab3dEf6hIj9kLm2nOp5qRs8tUv1wXy4z"),
        _j("Ab3dEf6hIj9kLm2nOp5qRs8tUv1wXy4z"),
    ),
    "anthropic_api_key": (
        _j("use ", "sk-", "ant-", "api03-", "Zz9Yy8Xx7Ww6Vv5Uu4Tt3Ss2"),
        _j("api03-", "Zz9Yy8Xx7Ww6Vv5Uu4Tt3Ss2"),
    ),
    "aws_access_key_id": (_j("id=", "AKIA", "QWERTYUIOP234567"), _j("AKIA", "QWERTYUIOP234567")),
    "aws_secret_access_key": (
        _j("aws_secret_access_key = ", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYzzSECRETKEY"[:40]),
        "wJalrXUtnFEMI/K7MDENG/bPxRfiCYzzSECRETKEY"[:40],
    ),
    "github_token": (_j("token ", "ghp", "_", "aB1" * 12), _j("ghp", "_", "aB1" * 12)),
    "slack_token": (
        _j("slack ", "xox", "b-", "1234567890-AbCdEfGhIjKl"),
        _j("xox", "b-", "1234567890-AbCdEfGhIjKl"),
    ),
    "slack_webhook": (
        _j("post to https://hooks.", "slack.com/services/", "T0001/B0002/SeCrEtPaTh123"),
        "SeCrEtPaTh123",
    ),
    "stripe_key": (
        _j("stripe ", "sk", "_live_", "4eC39HqLyjWDarjtT1zdp7dc"),
        _j("4eC39HqLyjWDarjtT1zdp7dc"),
    ),
    "google_api_key": (
        _j("g=", "AIza", "SyD-abcdefghijklmnopqrstuvwxyz12345"),
        _j("SyD-abcdefghijklmnopqrstuvwxyz12345"),
    ),
    "jwt": (
        _j(
            "jwt ",
            "eyJ",
            "hbGciOiJIUzI1NiJ9",
            ".",
            "eyJ",
            "zdWIiOiIxMjM0NTY3ODkwIn0",
            ".",
            "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
        ),
        "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
    ),
    "bearer_token": (
        _j("Authorization: Bearer ", "opaqueTok3nValue1234567"),
        "opaqueTok3nValue1234567",
    ),
    "pem_private_key": (
        _j(
            "-----BEGIN ",
            "RSA PRIVATE KEY-----\n",
            "MIIEowIBAAKCAQEAuniqueKeyBody42\n",
            "-----END RSA PRIVATE KEY-----",
        ),
        "MIIEowIBAAKCAQEAuniqueKeyBody42",
    ),
    "credit_card": (_j("card 4111 ", "1111 1111 1111 exp 12/30"), _j("4111 ", "1111 1111 1111")),
    "url_credentials": (_j("dsn postgres://admin:", "hunter2pass", "@db.local/app"), "hunter2pass"),
    "secret_assignment": (_j("login failed: password=", "Tr0ub4dor&3x"), "Tr0ub4dor"),
}


class ServingSession(Session):
    """A tiny replay-like session: serves recorded responses through ``spec.decode``."""

    mode: ClassVar[Literal["record", "replay"]] = "replay"

    def __init__(self, fixture: Fixture) -> None:
        self.calls = iter(fixture.calls())

    def _serve(self, spec: CallSpec) -> Any:
        ev = next(self.calls)
        assert (ev.kind, ev.name) == (spec.kind, spec.name)
        value = ev.response.value
        return spec.decode(value) if spec.decode is not None else value

    def call(self, spec: CallSpec, invoke: Callable[[], T]) -> T:
        result: T = self._serve(spec)
        return result

    async def acall(self, spec: CallSpec, invoke: Callable[[], Any]) -> Any:
        return self._serve(spec)

    def span(self, spec: CallSpec) -> AbstractContextManager[str]:
        @contextmanager
        def cm() -> Iterator[str]:
            yield "span"

        return cm()

    def finish(self, *, output: Any = None, error: BaseException | None = None) -> Fixture:
        raise NotImplementedError
