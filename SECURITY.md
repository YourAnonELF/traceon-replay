# Security policy

## Supported versions

Traceon Replay is in the 0.x series. Security fixes are released for the
latest minor version.

| Version | Supported |
|---|---|
| 0.1.x | Yes |

## Reporting a vulnerability

Please **do not open a public issue** for security problems.

Report them privately through GitHub Security Advisories:
<https://github.com/YourAnonELF/traceon-replay/security/advisories/new>
(repository **Security** tab → **Report a vulnerability**).

Please include:

- the affected version and how you installed it,
- a description of the issue and its impact,
- steps or a minimal fixture / script to reproduce it (remove any real secrets
  or personal data first).

We aim to acknowledge reports within 5 business days and to agree on a
disclosure timeline with you. Fixes are published as a new release with a
GitHub Security Advisory; we credit reporters unless you prefer otherwise.

## Scope

In scope, for example:

- a fixture that executes code, escapes the configured limits, or makes the
  loader or Studio read or write files outside the intended paths;
- secrets that the documented default redaction rules should remove but that
  end up in a fixture;
- replay reaching the real network or executing a real tool or model call
  without an explicit override or `block_network=False`;
- Studio exposing data beyond the fixtures passed on its command line, or
  being exploitable from a web page (CSRF, DNS rebinding, XSS);
- Langfuse credentials leaking into logs, errors or fixtures.

Out of scope:

- personal data that pattern-based redaction is documented not to detect;
- code paths that bypass the network guard as documented (uvloop, C
  extensions opening sockets natively);
- issues requiring an attacker to control the Python process or your
  `decode=` functions;
- Studio bound to a non-loopback interface with `--host` by the user.

See [docs/security-and-privacy.md](docs/security-and-privacy.md) for the
security model.
