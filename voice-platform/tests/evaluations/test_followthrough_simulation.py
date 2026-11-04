"""The follow-through reply belongs to the caller's turn in simulations too.

LiveKit's ``RunResult`` includes speech scheduled while the run is in progress, so the
follow-up's answer is part of the same turn (an earlier attempt to await it explicitly in the
simulator duplicated the reply — this test pins both properties).
"""

from __future__ import annotations

from tests.evaluations.test_evaluation_and_optimization import eval_config
from voice_platform.evaluation.scenario import Scenario
from voice_platform.evaluation.scripted import Reply, Rule, ScriptedLLM, after_tool, when_user
from voice_platform.evaluation.simulator import run_scenario


def narrating_model() -> ScriptedLLM:
    def follow_up(t: object) -> Reply | None:
        if getattr(t, "tool_name", None) is None and t.instruction_has("you would check"):  # type: ignore[attr-defined]
            return Reply(tool_calls=[("check_availability", {"date": "2030-01-08"})])
        return None

    return ScriptedLLM(
        [
            Rule(follow_up, "follow-up"),
            after_tool("check_availability", Reply(text="I have nine or ten in the morning.")),
            when_user(r"anything", Reply(text="Let me check that for you.")),
            when_user(r"thanks", Reply(text="Goodbye!")),
        ],
        ttft_s=0.5,  # like a real model: the follow-up is still generating when run() returns
    )


async def test_follow_up_reply_stays_in_the_same_turn() -> None:
    sc = Scenario.model_validate(
        {
            "id": "ft-sim",
            "name": "narrated lookup",
            "now": "2030-01-07T13:00:00+00:00",
            "script": [{"say": "Do you have anything on 2030-01-08?"}, {"say": "Thanks, bye."}],
        }
    )
    sim = await run_scenario(sc, eval_config(), agent_llm=narrating_model(), caller_llm=None)
    assert len(sim.turns) == 2
    assert sim.turns[0].agent.count("nine or ten") == 1, sim.turns[0].agent
    assert sim.turns[1].agent == "Goodbye!", sim.turns[1].agent
    assert [e["tool"] for e in sim.tool_events] == ["check_availability"]
