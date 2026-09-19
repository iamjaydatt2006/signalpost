"""Central request / cost / runtime / failure budget.

The evaluator runs a fixed batch under time, request, and cost constraints. This
module is the single accounting point so the agent can:

- keep safety headroom below evaluator limits,
- degrade gracefully (skip optional enrichment) instead of failing hard,
- still emit a terminal envelope for every remaining company, and
- report exact requests / cost / runtime in each envelope's ``operations`` block.

Crucially it separates **planned** (reserved by the controller's plan) from
**executed** (actually spent) budget. Planned figures never masquerade as real
spend: ``operations`` and the ``requests``/``cost_usd`` counters reflect *executed*
work only.

It is thread-safe because the batch runner enriches companies concurrently.
Reservations and commits are atomic under one lock.
"""
from __future__ import annotations

import threading
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any


@dataclass
class BudgetLimits:
    """Hard ceilings. ``None`` means unbounded for that dimension."""

    max_requests: int | None = None
    max_cost_usd: float | None = None
    max_runtime_seconds: float | None = None
    max_failures: int | None = None
    # Fraction of a limit at which optional work stops but mandatory work continues.
    headroom: float = 0.9

    def request_soft_cap(self) -> float | None:
        return None if self.max_requests is None else self.max_requests * self.headroom

    def cost_soft_cap(self) -> float | None:
        return None if self.max_cost_usd is None else self.max_cost_usd * self.headroom

    def runtime_soft_cap(self) -> float | None:
        return None if self.max_runtime_seconds is None else self.max_runtime_seconds * self.headroom


class Budget:
    """Thread-safe accumulator with soft (headroom) and hard limits."""

    def __init__(self, limits: BudgetLimits | None = None, *, clock=time.monotonic) -> None:
        self.limits = limits or BudgetLimits()
        self._clock = clock
        self._lock = threading.Lock()
        self._started = clock()
        # Executed (actual) spend.
        self.executed_requests = 0
        self.executed_cost_usd = 0.0
        # Planned (reserved) spend — never reported as actual.
        self.planned_requests = 0
        self.planned_cost_usd = 0.0
        self.failures = 0
        self.blocked = 0
        self.observations = 0
        self.requests_by_connector: Counter[str] = Counter()
        self.cost_by_connector: Counter[str] = Counter()
        self.degraded = False

    # -- backward-compatible aliases (executed semantics) -----------------
    @property
    def requests(self) -> int:
        return self.executed_requests

    @property
    def cost_usd(self) -> float:
        return self.executed_cost_usd

    # -- time -------------------------------------------------------------
    def elapsed_seconds(self) -> float:
        return self._clock() - self._started

    # -- limit checks -----------------------------------------------------
    def _hard_exceeded(self, base_req: int, base_cost: float, add_req: int, add_cost: float) -> bool:
        lim = self.limits
        if lim.max_requests is not None and base_req + add_req > lim.max_requests:
            return True
        if lim.max_cost_usd is not None and base_cost + add_cost > lim.max_cost_usd:
            return True
        if lim.max_runtime_seconds is not None and self.elapsed_seconds() > lim.max_runtime_seconds:
            return True
        if lim.max_failures is not None and self.failures >= lim.max_failures:
            return True
        return False

    def _soft_ok(self, base_req: int, base_cost: float, add_req: int, add_cost: float) -> bool:
        lim = self.limits
        rsc = lim.request_soft_cap()
        if rsc is not None and base_req + add_req > rsc:
            return False
        csc = lim.cost_soft_cap()
        if csc is not None and base_cost + add_cost > csc:
            return False
        tsc = lim.runtime_soft_cap()
        if tsc is not None and self.elapsed_seconds() > tsc:
            return False
        return True

    def can_spend(self, *, requests: int = 1, cost: float = 0.0, optional: bool = True) -> bool:
        """Check headroom against *executed* spend without recording."""
        with self._lock:
            if self._hard_exceeded(self.executed_requests, self.executed_cost_usd, requests, cost):
                return False
            if optional:
                return self._soft_ok(self.executed_requests, self.executed_cost_usd, requests, cost)
            return True

    # -- planning (reserve) ----------------------------------------------
    def reserve(self, *, requests: int = 1, cost: float = 0.0, connector: str = "unknown", optional: bool = True) -> bool:
        """Reserve *planned* budget for a task the controller intends to run.

        Reservations are checked against executed+planned so a plan cannot promise
        more than the budget allows, but they never increment executed spend.
        """
        with self._lock:
            base_req = self.executed_requests + self.planned_requests
            base_cost = self.executed_cost_usd + self.planned_cost_usd
            if self._hard_exceeded(base_req, base_cost, requests, cost):
                self.degraded = True
                return False
            if optional and not self._soft_ok(base_req, base_cost, requests, cost):
                self.degraded = True
                return False
            self.planned_requests += requests
            self.planned_cost_usd += cost
            return True

    # -- execution (commit) ----------------------------------------------
    def commit(self, *, requests: int = 0, cost: float = 0.0, connector: str = "unknown") -> bool:
        """Record *actual* executed spend. Returns False (recording nothing) when
        the commit would breach a hard limit, and flags degradation."""
        with self._lock:
            if self._hard_exceeded(self.executed_requests, self.executed_cost_usd, requests, cost):
                self.degraded = True
                return False
            self.executed_requests += requests
            self.executed_cost_usd += cost
            if requests:
                self.requests_by_connector[connector] += requests
            if cost:
                self.cost_by_connector[connector] += cost
            return True

    # -- backward-compatible spend API (executed semantics) --------------
    def try_spend(self, *, requests: int = 1, cost: float = 0.0, connector: str = "unknown", optional: bool = True) -> bool:
        """Atomically check headroom and commit as executed spend."""
        with self._lock:
            if self._hard_exceeded(self.executed_requests, self.executed_cost_usd, requests, cost):
                self.degraded = True
                return False
            if optional and not self._soft_ok(self.executed_requests, self.executed_cost_usd, requests, cost):
                self.degraded = True
                return False
            self.executed_requests += requests
            self.executed_cost_usd += cost
            self.requests_by_connector[connector] += requests
            if cost:
                self.cost_by_connector[connector] += cost
            return True

    def record_spend(self, *, requests: int = 0, cost: float = 0.0, connector: str = "unknown") -> None:
        """Unconditionally record executed spend that already happened
        (e.g. mandatory official fetches)."""
        with self._lock:
            self.executed_requests += requests
            self.executed_cost_usd += cost
            if requests:
                self.requests_by_connector[connector] += requests
            if cost:
                self.cost_by_connector[connector] += cost

    def record_failure(self, *, connector: str = "unknown") -> None:
        with self._lock:
            self.failures += 1

    def record_blocked(self, *, connector: str = "unknown") -> None:
        with self._lock:
            self.blocked += 1

    def record_observation(self) -> None:
        with self._lock:
            self.observations += 1

    def exhausted(self) -> bool:
        """True when not even one more mandatory request/second can proceed."""
        with self._lock:
            return self._hard_exceeded(self.executed_requests, self.executed_cost_usd, 1, 0.0)

    # -- reporting --------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                # Actual executed spend (authoritative for operations reporting).
                "requests": self.executed_requests,
                "third_party_cost_usd": round(self.executed_cost_usd, 6),
                "executed_requests": self.executed_requests,
                "executed_cost_usd": round(self.executed_cost_usd, 6),
                # Planning-only reservations, kept strictly separate.
                "planned_requests": self.planned_requests,
                "planned_cost_usd": round(self.planned_cost_usd, 6),
                "runtime_ms": int(self.elapsed_seconds() * 1000),
                "failures": self.failures,
                "blocked_sources": self.blocked,
                "successful_observations": self.observations,
                "requests_by_connector": dict(self.requests_by_connector),
                "cost_by_connector": {k: round(v, 6) for k, v in self.cost_by_connector.items()},
                "degraded": self.degraded,
                "limits": {
                    "max_requests": self.limits.max_requests,
                    "max_cost_usd": self.limits.max_cost_usd,
                    "max_runtime_seconds": self.limits.max_runtime_seconds,
                    "max_failures": self.limits.max_failures,
                    "headroom": self.limits.headroom,
                },
            }
