"""Running the checks and reporting the result."""
from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path

from bsetl.logconfig import get_logger
from bsetl.quality.checks import (
    CHECKS,
    CheckResult,
    Severity,
    Thresholds,
    check_schema,
    check_season_window,
    check_skill_provenance,
)
from bsetl.transform.seasons import season_for_database

logger = get_logger(__name__)

_SYMBOL = {
    Severity.OK: "ok  ",
    Severity.WARN: "warn",
    Severity.FAIL: "FAIL",
    Severity.SKIP: "--  ",
}


@dataclass
class QualityReport:
    db_path: str
    season: str
    results: list[CheckResult] = field(default_factory=list)

    def _of(self, severity: Severity) -> list[CheckResult]:
        return [r for r in self.results if r.severity is severity]

    @property
    def failures(self) -> list[CheckResult]:
        return self._of(Severity.FAIL)

    @property
    def warnings(self) -> list[CheckResult]:
        return self._of(Severity.WARN)

    @property
    def ok(self) -> bool:
        """True when nothing failed. Warnings do not block publication."""
        return not self.failures

    def to_dict(self) -> dict:
        return {
            "database": self.db_path,
            "season": self.season,
            "ok": self.ok,
            "counts": {
                s.value: len(self._of(s))
                for s in (Severity.OK, Severity.WARN, Severity.FAIL, Severity.SKIP)
            },
            "results": [asdict(r) | {"severity": r.severity.value} for r in self.results],
        }

    def render(self) -> str:
        width = max((len(r.name) for r in self.results), default=10)
        lines = [f"Quality report for {self.season}  ({Path(self.db_path).name})", ""]
        lines += [
            f"  [{_SYMBOL[r.severity]}] {r.name:<{width}}  {r.message}"
            for r in self.results
        ]
        counts = self.to_dict()["counts"]
        lines += [
            "",
            f"  {counts['ok']} passed, {counts['warn']} warning(s), "
            f"{counts['fail']} failure(s), {counts['skip']} skipped",
            "",
            "PASS — safe to publish" if self.ok else "FAIL — not safe to publish",
        ]
        return "\n".join(lines)


def run_quality_checks(
    db_path: str,
    *,
    season: str | None = None,
    thresholds: Thresholds | None = None,
) -> QualityReport:
    """Run every check against a season database.

    A check that raises is reported as a failure rather than aborting the run:
    a gate that crashes tells you less than one that says which check broke.
    """
    thresholds = thresholds or Thresholds()
    season = season or season_for_database(db_path) or "unknown"
    report = QualityReport(db_path=db_path, season=season)

    if not Path(db_path).exists():
        report.results.append(
            CheckResult("database", Severity.FAIL, f"No such database: {db_path}")
        )
        return report

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        # Everything downstream assumes the table exists and has core columns.
        schema = check_schema(conn, thresholds)
        report.results.append(schema)
        if schema.severity is Severity.FAIL:
            return report

        for check in CHECKS:
            if check is check_schema:
                continue
            try:
                out = check(conn, thresholds)
            except Exception as e:
                logger.exception("Check %s raised", getattr(check, "__name__", check))
                out = CheckResult(
                    getattr(check, "__name__", "unknown").removeprefix("check_"),
                    Severity.FAIL, f"Check raised {type(e).__name__}: {e}",
                )
            report.results.extend(out if isinstance(out, list) else [out])

        try:
            report.results.append(check_season_window(conn, thresholds, db_path))
        except Exception as e:
            report.results.append(
                CheckResult("season_window", Severity.FAIL,
                            f"Check raised {type(e).__name__}: {e}")
            )

        try:
            report.results.append(check_skill_provenance(conn, thresholds, season))
        except Exception as e:
            report.results.append(
                CheckResult("skill_provenance", Severity.FAIL,
                            f"Check raised {type(e).__name__}: {e}")
            )
    finally:
        conn.close()
    return report
