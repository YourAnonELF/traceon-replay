"""Optional integrations with third-party tracing systems.

Integrations only *read* data produced elsewhere and convert it into Traceon
fixtures. They never add runtime dependencies: everything here uses the standard
library plus Traceon's own core.

* :mod:`traceon.integrations.langfuse` -- import a Langfuse trace (from an export
  file or the Langfuse public API) as a fixture.
"""

from __future__ import annotations

__all__ = ["langfuse"]
