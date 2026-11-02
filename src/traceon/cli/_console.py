"""Console setup, shared CLI state and error types for the ``traceon`` command."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Literal, TextIO

from rich.console import Console
from rich.text import Text

from traceon.diff.render import ASCII, STYLES, UNICODE, Glyphs, supports_unicode

ColorMode = Literal["auto", "always", "never"]

#: Exit status when every check passed.
EXIT_OK = 0
#: Exit status for a divergence, a regression or a failed run.
EXIT_DIVERGED = 1
#: Exit status for usage errors and unreadable or invalid fixtures.
EXIT_ERROR = 2


class CliError(Exception):
    """An expected, user-facing failure: printed as ``error: <message>``, exit 2."""

    def __init__(
        self, message: str, *, hint: str | None = None, exit_code: int = EXIT_ERROR
    ) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.exit_code = exit_code


def make_console(stream: TextIO, color: ColorMode) -> Console:
    """A console that honors ``--color``, ``NO_COLOR`` and non-TTY output.

    In ``auto`` mode color is used only on terminals, and never when ``NO_COLOR``
    is set. Output written to pipes or files uses a stable width (``COLUMNS`` or
    100) so logs and snapshots do not depend on the invoking terminal.
    """
    is_tty = bool(getattr(stream, "isatty", lambda: False)())
    width: int | None = None
    if not is_tty or color == "never":
        try:
            width = int(os.environ.get("COLUMNS", "100"))
        except ValueError:
            width = 100
    if color == "always":
        return Console(
            file=stream,
            force_terminal=True,
            no_color=False,
            width=width,
            highlight=False,
            emoji=False,
            soft_wrap=False,
        )
    if color == "never" or os.environ.get("NO_COLOR"):
        return Console(
            file=stream,
            force_terminal=False,
            no_color=True,
            color_system=None,
            width=width,
            highlight=False,
            emoji=False,
            soft_wrap=False,
        )
    return Console(
        file=stream,
        force_terminal=is_tty,
        width=width,
        highlight=False,
        emoji=False,
        soft_wrap=False,
    )


@dataclass
class CliState:
    """Per-invocation settings, stored on the click context object."""

    debug: bool = False
    color: ColorMode = "auto"
    out: Console = field(default_factory=lambda: make_console(sys.stdout, "auto"))
    err: Console = field(default_factory=lambda: make_console(sys.stderr, "auto"))
    glyphs: Glyphs = UNICODE

    @classmethod
    def create(cls, *, debug: bool, color: ColorMode) -> CliState:
        stdout, stderr = sys.stdout, sys.stderr
        return cls(
            debug=debug,
            color=color,
            out=make_console(stdout, color),
            err=make_console(stderr, color),
            glyphs=UNICODE if supports_unicode(stdout) else ASCII,
        )

    def error(self, message: str, hint: str | None = None) -> None:
        """Print ``error: message`` (and an optional hint) to stderr."""
        self.err.print(
            Text.assemble(("error", STYLES["fail"] + " bold"), ": ", message), soft_wrap=True
        )
        if hint:
            self.err.print(Text.assemble(("  hint", STYLES["muted"]), ": ", hint), soft_wrap=True)

    def warn(self, message: str) -> None:
        """Print ``warning: message`` to stderr."""
        self.err.print(
            Text.assemble(("warning", STYLES["changed"] + " bold"), ": ", message), soft_wrap=True
        )
