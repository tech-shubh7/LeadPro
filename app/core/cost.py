"""Per-job spend tracking with a hard cap. Every paid call goes through record()."""
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

    @property
    def spent_inr(self) -> float:
        return sum(line.cost_inr for line in self.lines)

    def ensure_budget(self, next_cost_inr: float) -> None:
        """Call before a paid request; refuses calls that would cross the cap."""
        if self.spent_inr + next_cost_inr > self.cap_inr:
            raise CostCapExceeded(
                f"cost cap ₹{self.cap_inr:.2f} would be exceeded (spent ₹{self.spent_inr:.2f}, next ₹{next_cost_inr:.2f})"
            )

    def record(self, provider: str, operation: str, units: float, cost_inr: float) -> None:
        self.lines.append(UsageLine(provider, operation, units, cost_inr))
