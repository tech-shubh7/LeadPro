"""Per-job spend tracking with a hard cap. Every paid call goes through reserve() + record().

Concurrent workers share one tracker. A check-then-spend without a reservation lets every worker pass
the check before any of them records, overshooting the cap by up to (concurrency x call price); so a
paid call reserves its estimated price first, and the reservation is released when the call ends.
"""
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field


class CostCapExceeded(RuntimeError):
    pass


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

    @property
    def spent_inr(self) -> float:
        return sum(line.cost_inr for line in self.lines)

    def ensure_budget(self, next_cost_inr: float) -> None:
        """Refuses a call that would cross the cap, counting calls still in flight."""
        if self.spent_inr + self.reserved_inr + next_cost_inr > self.cap_inr:
            raise CostCapExceeded(
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

    def record(self, provider: str, operation: str, units: float, cost_inr: float) -> None:
        self.lines.append(UsageLine(provider, operation, units, cost_inr))
