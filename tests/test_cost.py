import pytest

from app.exceptions.custom_exceptions import CostCapExceededError
from app.utils.cost_tracker import CostTracker


def test_cap_blocks_the_call_that_would_cross_it():
    t = CostTracker(cap_inr=50)
    t.ensure_budget(30)
    t.record("serper", "search", 300, 30)
    t.ensure_budget(19.5)
    with pytest.raises(CostCapExceededError):
        t.ensure_budget(25)
    assert t.spent_inr == 30


def test_a_call_waits_for_in_flight_calls_instead_of_failing():
    """Job 20 stopped with ₹9 of ₹60 spent: worst-case reservations, not spending, filled the cap."""
    import asyncio

    tracker = CostTracker(cap_inr=10)

    async def call(actual: float):
        async with tracker.hold(6):  # each could cost 6, really costs `actual`
            await asyncio.sleep(0.01)
            tracker.record("x", "y", 1, actual)

    async def main():
        await asyncio.gather(*(call(1) for _ in range(5)))

    asyncio.run(main())
    assert tracker.spent_inr == 5  # all five ran, one or two at a time
    assert tracker.reserved_inr == 0


def test_a_call_that_cannot_fit_is_still_refused():
    import asyncio

    tracker = CostTracker(cap_inr=10)
    tracker.record("x", "y", 1, 8)

    async def main():
        async with tracker.hold(5):
            pass

    with pytest.raises(CostCapExceededError):
        asyncio.run(main())
