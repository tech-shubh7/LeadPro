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
from contextvars import ContextVar
from dataclasses import dataclass, field

from app.exceptions.custom_exceptions import CostCapExceededError


@dataclass
class UsageLine:
    provider: str
    operation: str
    units: float
    cost_inr: float
    # What the call was for, from usage_tags(): the pipeline step ("extract", "score", ...), the discovery
    # channel of the company being researched, and its domain. Empty on lines recorded before tagging.
    stage: str = ""
    channel: str = ""
    domain: str = ""
    # Model calls only: where the tokens went. Output costs 5x input, and reasoning is billed as output, so
    # the split (not the total) says what to cut. Zero on non-model lines and on lines saved before the split.
    input_tokens: int = 0
    cached_tokens: int = 0  # part of input_tokens, billed at the cached price
    output_tokens: int = 0  # includes reasoning_tokens
    reasoning_tokens: int = 0


@dataclass(frozen=True)
class TokenSplit:
    input: int = 0
    cached: int = 0
    output: int = 0  # visible answer + reasoning
    reasoning: int = 0


# Set by the run around each step and each company. A context variable, so every concurrent worker (an
# asyncio task with its own copy of the context) tags its own calls without passing anything down.
_TAGS: ContextVar[dict[str, str]] = ContextVar("usage_tags", default={})  # noqa: B039 - never mutated


# Set by the tracing wrapper around one model call: every usage line recorded during that call is also
# collected here, so the trace carries that call's exact tokens and price (not a neighbour's).
_CAPTURE: ContextVar[list | None] = ContextVar("usage_capture", default=None)


def current_tags() -> dict[str, str]:
    return dict(_TAGS.get())


@contextmanager
def capture_usage() -> Iterator[list]:
    lines: list = []
    token = _CAPTURE.set(lines)
    try:
        yield lines
    finally:
        _CAPTURE.reset(token)


@contextmanager
def usage_tags(**tags: str) -> Iterator[None]:
    """Tags every usage line recorded inside the block (nested blocks add to or override outer tags)."""
    token = _TAGS.set({**_TAGS.get(), **tags})
    try:
        yield
    finally:
        _TAGS.reset(token)


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

    def record(
        self, provider: str, operation: str, units: float, cost_inr: float, tokens: TokenSplit | None = None
    ) -> None:
        tags = _TAGS.get()
        split = tokens or TokenSplit()
        line = UsageLine(
            provider,
            operation,
            units,
            cost_inr,
            tags.get("stage", ""),
            tags.get("channel", ""),
            tags.get("domain", ""),
            split.input,
            split.cached,
            split.output,
            split.reasoning,
        )
        self.lines.append(line)
        if (captured := _CAPTURE.get()) is not None:
            captured.append(line)
