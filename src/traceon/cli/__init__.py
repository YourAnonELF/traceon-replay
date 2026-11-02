"""The ``traceon`` command-line interface (click + rich).

Commands: ``inspect``, ``validate``, ``record``, ``replay``, ``diff``,
``import langfuse`` and ``studio``. Exit status is ``0`` on success, ``1`` on a
divergence, regression or failed run, and ``2`` on usage or fixture errors.
"""

from traceon.cli._console import EXIT_DIVERGED, EXIT_ERROR, EXIT_OK
from traceon.cli.app import main

__all__ = ["EXIT_DIVERGED", "EXIT_ERROR", "EXIT_OK", "main"]
