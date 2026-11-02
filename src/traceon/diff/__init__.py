"""Compare two recorded runs and summarize their metrics.

    from traceon.core.io import load_fixture
    from traceon.diff import compare

    report = compare(load_fixture("baseline.json"), load_fixture("candidate.json"),
                     ignore_paths=["$..request_id"])
    if not report.equivalent:
        print(report.first_divergence.message)

Rendering helpers for terminals, CI logs and pull-request comments live in
:mod:`traceon.diff.render`.
"""

from traceon.diff.engine import IgnoreRules, compare, label, metrics

__all__ = ["IgnoreRules", "compare", "label", "metrics"]
