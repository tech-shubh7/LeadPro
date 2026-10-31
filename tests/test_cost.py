import pytest

from app.core.cost import CostCapExceeded, CostTracker


def test_cap_blocks_the_call_that_would_cross_it():
    t = CostTracker(cap_inr=50)
    t.ensure_budget(30); t.record("serper", "search", 300, 30)
    t.ensure_budget(19.5)
    with pytest.raises(CostCapExceeded):
        t.ensure_budget(25)
    assert t.spent_inr == 30
