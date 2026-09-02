"""Bounding a crawl.

An unattended run needs to stop on its own, for a reason it can name. Three
things end a crawl early: it has spent its request allowance, it has run out of
time, or it has stopped finding anything new. The last one matters most — BFS
neighborhoods saturate, and a crawl that keeps requesting after yield collapses
is buying rows it already owns.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum


class StopReason(StrEnum):
    FRONTIER_EXHAUSTED = "frontier_exhausted"
    REQUEST_BUDGET = "request_budget"
    TIME_BUDGET = "time_budget"
    YIELD_COLLAPSED = "yield_collapsed"
    FAILED = "failed"


class Outcome(StrEnum):
    """What came back from one API request."""

    OK = "ok"
    NOT_FOUND = "not_found"
    RATE_LIMITED = "rate_limited"
    ERROR = "error"


@dataclass(frozen=True)
class RunBudget:
    """Limits for a single crawl. `None` disables that limit."""

    max_requests: int | None = None
    max_seconds: float | None = None

    # Stop when the trailing window yields fewer than this many newly inserted
    # rows per 1000 requests.
    min_rows_per_1k_requests: float | None = None
    # Requests to spend before yield is judged at all. A crawl opens slowly:
    # the first batch discovers tags but has not written rows yet.
    yield_grace_requests: int = 2_000
    # Width of the trailing window used to measure yield.
    yield_window_requests: int = 2_000

    def __post_init__(self) -> None:
        for name in ("max_requests", "yield_grace_requests", "yield_window_requests"):
            v = getattr(self, name)
            if v is not None and v <= 0:
                raise ValueError(f"{name} must be positive, got {v}")
        if self.max_seconds is not None and self.max_seconds <= 0:
            raise ValueError(f"max_seconds must be positive, got {self.max_seconds}")
        if self.min_rows_per_1k_requests is not None and self.min_rows_per_1k_requests < 0:
            raise ValueError("min_rows_per_1k_requests must be non-negative")


@dataclass
class CrawlStats:
    """Live counters for one crawl, and the budget decision built on them."""

    budget: RunBudget = field(default_factory=RunBudget)

    requests_made: int = 0
    rows_inserted: int = 0
    tags_fetched: int = 0
    parse_failures: int = 0
    malformed_records: int = 0
    outcomes: dict[str, int] = field(default_factory=dict)

    _started: float = field(default_factory=time.monotonic)
    # (requests_made, rows_inserted) snapshots, for the trailing yield window
    _samples: deque[tuple[int, int]] = field(default_factory=deque)

    def __post_init__(self) -> None:
        # Anchor the window at the origin. Without this, a crawl that inserts
        # nothing at all never records a sample, so yield stays unmeasurable
        # and collapse can never fire — the exact case worth catching.
        if not self._samples:
            self._samples.append((0, 0))

    # -- recording -------------------------------------------------------
    def record_request(self, outcome: Outcome) -> None:
        self.requests_made += 1
        self.outcomes[outcome.value] = self.outcomes.get(outcome.value, 0) + 1
        if outcome is Outcome.OK:
            self.tags_fetched += 1

    def record_rows(self, n: int) -> None:
        """Record rows actually inserted — not rows attempted.

        Writes go through INSERT OR IGNORE, so attempted and inserted diverge
        sharply on a warm database. Yield is only meaningful on the latter.
        """
        self.rows_inserted += n
        self._samples.append((self.requests_made, self.rows_inserted))
        self._prune_samples()

    def record_parse_failure(self, n: int = 1) -> None:
        self.parse_failures += n

    def record_malformed(self, n: int = 1) -> None:
        """A set whose record could not describe a real match; dropped."""
        self.malformed_records += n

    # -- derived ---------------------------------------------------------
    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self._started

    def _prune_samples(self) -> None:
        window = self.budget.yield_window_requests
        # Keep the oldest sample that is still at least a full window behind,
        # so the window never collapses to nothing.
        while len(self._samples) > 1 and self.requests_made - self._samples[1][0] >= window:
            self._samples.popleft()

    def recent_yield_per_1k(self) -> float | None:
        """Rows inserted per 1000 requests over the trailing window.

        Returns None until a full window of requests has been observed.
        """
        if not self._samples:
            return None
        req0, rows0 = self._samples[0]
        span = self.requests_made - req0
        if span < self.budget.yield_window_requests:
            return None
        return (self.rows_inserted - rows0) * 1000.0 / span

    # -- the decision ----------------------------------------------------
    def should_stop(self) -> StopReason | None:
        b = self.budget
        if b.max_requests is not None and self.requests_made >= b.max_requests:
            return StopReason.REQUEST_BUDGET
        if b.max_seconds is not None and self.elapsed_seconds >= b.max_seconds:
            return StopReason.TIME_BUDGET
        if (
            b.min_rows_per_1k_requests is not None
            and self.requests_made >= b.yield_grace_requests
        ):
            y = self.recent_yield_per_1k()
            if y is not None and y < b.min_rows_per_1k_requests:
                return StopReason.YIELD_COLLAPSED
        return None

    def summary(self) -> dict[str, object]:
        y = self.recent_yield_per_1k()
        return {
            "requests_made": self.requests_made,
            "rows_inserted": self.rows_inserted,
            "tags_fetched": self.tags_fetched,
            "parse_failures": self.parse_failures,
            "malformed_records": self.malformed_records,
            "elapsed_seconds": round(self.elapsed_seconds, 1),
            "recent_yield_per_1k": None if y is None else round(y, 1),
            **{f"http_{k}": v for k, v in sorted(self.outcomes.items())},
        }
