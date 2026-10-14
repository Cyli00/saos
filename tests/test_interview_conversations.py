"""模型编写的受控访谈回放；事实映射显式给出，不冒充独立模型盲测。"""

import json
from pathlib import Path

import pytest

from saos.recipes.microscope import interview

SCENARIOS = json.loads(
    (Path(__file__).parent / "fixtures/interview/conversations.json").read_text(
        encoding="utf-8"
    )
)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s["id"] for s in SCENARIOS])
def test_multiturn_interview_matches_declared_facts(scenario):
    draft = {
        "schema": 1,
        "request": "",
        "scope": {"medium": "air", "conjugate": "infinity"},
        "spec": {},
        "provenance": {},
        "requirements": [],
        "questions": [],
    }
    requirements = {}
    for turn in scenario["turns"]:
        draft["request"] += turn["user"] + "\n"
        if "scope" in turn:
            draft["scope"] = turn["scope"]
        for path, value in turn["facts"].items():
            interview.assign(draft["spec"], path, value)
            draft["provenance"][path] = {"source": "user", "basis": turn["user"]}
            requirements[path] = {
                "text": turn["user"],
                "kind": "target" if path.startswith("targets.") else "constraint",
                "fields": [path],
            }
        if turn.get("unresolved"):
            requirements["unresolved"] = {
                "text": turn["unresolved"],
                "kind": "unresolved",
                "fields": [],
            }
        draft["requirements"] = list(requirements.values())
        result = interview.check(draft)
        assert result["ready"] == turn["ready"]
        if "question_contains" in turn:
            assert any(turn["question_contains"] in q for q in result["questions"])
    flat = interview.flatten(result["spec"])
    for path, value in scenario["expected"].items():
        assert flat[path] == value
    for path in interview.flatten(draft["spec"]):
        field = next(f for f in result["fields"] if f["path"] == path)
        assert field["source"] == "user"
        assert field["basis"] in draft["request"]
