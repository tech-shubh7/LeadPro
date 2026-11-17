import asyncio
import json

from app.providers.llm.manual_provider import ManualLLMProvider
from app.services.scoring_service import ScoreOut
from app.utils.cost_tracker import CostTracker

GOOD = {
    "fit_score": 30,
    "intent_score": 30,
    "timing_score": 10,
    "why_this_lead": "x [E1]",
    "outreach_angle": "y",
    "risks": "z",
    "cited_evidence_ids": ["E1"],
}


def test_manual_answer_is_validated_and_bad_answers_rejected(tmp_path):
    cost = CostTracker(cap_inr=1)
    llm = ManualLLMProvider(cost, task_dir=tmp_path, poll_s=0.01, timeout_s=5)

    async def answer():
        while not list(tmp_path.glob("*.task.json")):  # noqa: ASYNC110 - polling for a file the provider writes
            await asyncio.sleep(0.01)
        task = next(tmp_path.glob("*.task.json"))
        task_id = json.loads(task.read_text())["id"]
        (tmp_path / f"{task_id}.response.json").write_text('{"fit_score": "lots"}')  # invalid -> rejected
        while not (tmp_path / f"{task_id}.error.txt").exists():  # noqa: ASYNC110 - polling for a file the provider writes
            await asyncio.sleep(0.01)
        (tmp_path / f"{task_id}.response.json").write_text(json.dumps(GOOD))

    async def main():
        result, _ = await asyncio.gather(llm.parse("m", "score it", "<evidence>", ScoreOut), answer())
        return result

    result = asyncio.run(main())
    assert result.fit_score == 30
    assert cost.spent_inr == 0
    assert not list(tmp_path.glob("*.task.json"))  # task cleared once answered
    # Same request again is answered instantly from the saved response (free reruns).
    again = asyncio.run(llm.parse("m", "score it", "<evidence>", ScoreOut))
    assert again.intent_score == 30
