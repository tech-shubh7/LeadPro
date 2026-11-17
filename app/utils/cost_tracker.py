"""Per-job spend tracking with a hard cap. Every paid call goes through hold() + record().

Concurrent workers share one tracker. A check-then-spend without a reservation lets every worker pass
the check before any of them records, overshooting the cap by up to (concurrency x call price); so a
paid call reserves its maximum price first, and the reservation is released when the call ends.

A call that does not fit while others are in flight WAITS for them rather than failing: reservations
are worst cases, and most of what they hold is released unspent. Refusing there stopped a run with ₹9
of a ₹60 cap spent (job 20). A call is refused only when it could not fit even if nothing else were in
flight, so the cap is still never crossed.
"""

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field

from app.exceptions.custom_exceptions import CostCapExceededError


@dataclass
class UsageLine:
    provider: str
    operation: str
    units: float
    cost_inr: float


@dataclass
class CostTracker:
    cap_inr: float
    lines: list[UsageLine] = field(default_factory=list)
    reserved_inr: float = 0.0  # estimated price of paid calls currently in flight
    _settled: asyncio.Condition | None = field(default=None, repr=False, compare=False)

    @property
    def spent_inr(self) -> float:
        return sum(line.cost_inr for line in self.lines)

    def ensure_budget(self, next_cost_inr: float) -> None:
        """Refuses a call that would cross the cap, counting calls still in flight."""
        if self.spent_inr + self.reserved_inr + next_cost_inr > self.cap_inr:
            raise CostCapExceededError(
                f"cost cap ₹{self.cap_inr:.2f} would be exceeded (spent ₹{self.spent_inr:.2f}, "
                f"in flight ₹{self.reserved_inr:.2f}, next ₹{next_cost_inr:.2f})"
            )

    @contextmanager
    def reserve(self, estimate_inr: float) -> Iterator[None]:
        """Wrap a paid call: checks the budget and holds `estimate_inr` until the call finishes
        (record() the actual price inside the block)."""
        self.ensure_budget(estimate_inr)
        self.reserved_inr += estimate_inr
        try:
            yield
        finally:
            self.reserved_inr -= estimate_inr

    def _refuse_if_impossible(self, estimate_inr: float) -> None:
        if self.spent_inr + estimate_inr > self.cap_inr:
            raise CostCapExceededError(
                f"cost cap ₹{self.cap_inr:.2f} reached (spent ₹{self.spent_inr:.2f}, next call up to "
                f"₹{estimate_inr:.2f})"
            )

    @asynccontextmanager
    async def hold(self, estimate_inr: float) -> AsyncIterator[None]:
        """Async reservation for a paid call: waits while calls in flight hold the budget it needs, and
        refuses only when the call cannot fit at all. record() the actual price inside the block."""
        if self._settled is None:
            self._settled = asyncio.Condition()
        async with self._settled:
            await self._settled.wait_for(
                lambda: (
                    self.spent_inr + self.reserved_inr + estimate_inr <= self.cap_inr
                    or self.spent_inr + estimate_inr > self.cap_inr
                )
            )
            self._refuse_if_impossible(estimate_inr)
            self.reserved_inr += estimate_inr
        try:
            yield
        finally:
            async with self._settled:
                self.reserved_inr -= estimate_inr
                self._settled.notify_all()

    def record(self, provider: str, operation: str, units: float, cost_inr: float) -> None:
        self.lines.append(UsageLine(provider, operation, units, cost_inr))
