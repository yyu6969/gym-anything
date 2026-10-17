"""Reference judging keeps absence, failure and malformed output distinct."""

import asyncio
import copy

import pytest

from gym_anything.verification.vlm_checklist import (
    REFERENCE_EVIDENCE_POLICY, build_verification_prompt, compute_binary_scores,
    evaluate_reference_checklist, validate_reference_verdicts,
    validate_reference_evidence,
)


def checklist(scope="terminal"):
    return {"scoring_policy": REFERENCE_EVIDENCE_POLICY,
            "items": [{"id": "content", "criterion": "The saved file contains the required phrase.", "evidence_scope": scope}]}


def evidence():
    return [{"id": "earlier", "kind": "image", "path": "early.png", "availability": "available", "scope": "trajectory"},
            {"id": "final", "kind": "image", "path": "final.png", "availability": "available", "scope": "terminal"},
            {"id": "export", "kind": "value", "value": "state", "availability": "available", "scope": "auxiliary"}]


def verdict(status="PASS", refs=None):
    return {"items": [{"id": "content", "status": status, "evidence_refs": ["final"] if refs is None else refs,
                       "reasoning": "Observed the required state."}]}


@pytest.mark.parametrize("status,score", [("PASS", 1), ("FAIL", 0), ("UNOBSERVABLE", None), ("ERROR", None)])
def test_reference_status_scores(status, score):
    result = asyncio.run(evaluate_reference_checklist(checklist=checklist(), evidence=evidence(), task_description="Task",
                         query=lambda **kwargs: {"success": True, "parsed": verdict(status)}))
    assert result["items"][0]["score"] == score
    assert result["items"][0]["status"] == status


@pytest.mark.parametrize("mutation", ["duplicate", "missing", "unknown", "invalid_status", "bad_ref", "early_ref", "no_ref", "malformed"])
def test_invalid_judge_results_are_errors(mutation):
    response = verdict()
    if mutation == "duplicate":
        response["items"].append(copy.deepcopy(response["items"][0]))
    elif mutation == "missing":
        response["items"] = []
    elif mutation == "unknown":
        response["items"][0]["id"] = "other"
    elif mutation == "invalid_status":
        response["items"][0]["status"] = "partial"
    elif mutation == "bad_ref":
        response["items"][0]["evidence_refs"] = ["nonexistent"]
    elif mutation == "early_ref":
        response["items"][0]["evidence_refs"] = ["earlier"]
    elif mutation == "no_ref":
        response["items"][0]["evidence_refs"] = []
    else:
        response = {"items": "invalid"}
    result = asyncio.run(evaluate_reference_checklist(checklist=checklist(), evidence=evidence(), task_description="Task",
                         query=lambda **kwargs: {"success": True, "parsed": response}))
    assert result["items"][0]["status"] == "ERROR" and result["items"][0]["score"] is None


def test_missing_terminal_does_not_use_earlier_success():
    calls = []
    def query(**kwargs):
        calls.append(kwargs)
        return {"success": True, "parsed": verdict()}
    result = asyncio.run(evaluate_reference_checklist(checklist=checklist(), evidence=evidence()[:1], task_description="Task", query=query))
    assert result["items"][0]["status"] == "UNOBSERVABLE" and calls == []


@pytest.mark.parametrize("scope,admissible", [("terminal", ["final"]), ("trajectory", ["earlier", "final"]), ("auxiliary", ["export"])])
def test_scope_selects_and_validates_evidence(scope, admissible):
    calls = []
    def query(**kwargs):
        calls.append(kwargs)
        return {"success": True, "parsed": verdict(refs=admissible)}
    result = asyncio.run(evaluate_reference_checklist(checklist=checklist(scope), evidence=evidence(), task_description="Task", query=query))
    assert result["items"][0]["status"] == "PASS"
    assert [item["id"] for item in calls[0]["evidence"]] == admissible


def test_default_binary_policy_remains_fail_closed():
    original = {"schema_version": "binary_outcome_checklist", "scoring_policy": "aab_binary_outcome_v1",
                "items": [{"id": "a", "criterion": "A", "scoring": "binary"}, {"id": "b", "criterion": "B", "scoring": "binary"}]}
    scores = compute_binary_scores({"items": [{"id": "a", "verdict": "PASS"}]}, original)
    assert scores["item_scores"] == [1, 0] and scores["reward"] == 0
    prompt = build_verification_prompt(checklist=original, task_description="Task", image_count=2)
    assert "FAIL applies to incomplete, missing, blocked or uncertain outcomes" in prompt
    assert "UNOBSERVABLE" not in prompt


def test_reference_policy_is_opt_in():
    value = checklist()
    value.pop("scoring_policy")
    with pytest.raises(ValueError, match="explicitly"):
        validate_reference_verdicts(verdict(), value, evidence())


def test_evidence_manifest_cannot_alias_or_omit_provenance():
    records = evidence()
    with pytest.raises(ValueError, match="unique"):
        validate_reference_evidence(records + records[:1])
    records[0].pop("scope")
    with pytest.raises(ValueError, match="scope"):
        validate_reference_evidence(records)
