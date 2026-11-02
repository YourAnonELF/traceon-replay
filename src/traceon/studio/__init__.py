"""Traceon Studio: a local, read-only web UI for inspecting and diffing fixtures.

Start it with ``traceon studio path/to/fixtures`` or :func:`traceon.studio.server.serve`.
The server binds to the loopback interface by default, serves only the fixture
files it was given, and makes no external network requests.
"""

from __future__ import annotations

from traceon.studio.server import FixtureRegistry, StudioApp, StudioError, make_server, serve

__all__ = ["FixtureRegistry", "StudioApp", "StudioError", "make_server", "serve"]
