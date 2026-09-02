"""Data quality checks that gate publication.

An automated pipeline publishes whatever it produced, so something has to
decide whether what it produced is fit to publish.
"""

from bsetl.quality.checks import CheckResult, Severity, Thresholds
from bsetl.quality.report import QualityReport, run_quality_checks

__all__ = [
    "CheckResult",
    "QualityReport",
    "Severity",
    "Thresholds",
    "run_quality_checks",
]
