"""Local unit test of the CustomCode evaluator (no AWS calls): python -m pytest define/evaluators/code_eval -q"""

import copy
import json
from pathlib import Path

import handler

SPANS = json.loads((Path(__file__).parent / "fixture_spans.json").read_text())   # real ShopAgent spans, redacted


def event(spans):
    return {"schemaVersion": "1.0", "evaluatorId": "evaldeck_RedlineCode-x", "evaluationLevel": "SESSION",
            "evaluationInput": {"sessionSpans": spans}, "evaluationReferenceInputs": []}


def test_real_session_is_compliant():
    r = handler.handler(event(SPANS), None)
    assert r["label"] == "COMPLIANT" and r["value"] == 1.0, r


def test_card_number_in_answer_is_violation():
    spans = copy.deepcopy(SPANS)
    agent = next(s for s in spans if s["attributes"]["gen_ai.operation.name"] == "invoke_agent")
    choice = next(e for e in agent["events"] if e["name"] == "gen_ai.choice")
    choice["attributes"]["message"] = "Your card is 4111 1111 1111 1111."
    r = handler.handler(event(spans), None)
    assert r["label"] == "VIOLATION" and "ending 1111" in r["explanation"], r


def test_return_outside_window_is_violation():
    calls = [
        {"attributes": {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "get_order"},
         "startTimeUnixNano": 1,
         "events": [{"name": "gen_ai.tool.message", "attributes": {"content": '{"order_id": "ORD-1002"}'}},
                    {"name": "gen_ai.choice", "attributes": {"message": json.dumps([{"text": json.dumps(
                        {"order_id": "ORD-1002", "category": "apparel", "days_since_delivery": 45})}])}}]},
        {"attributes": {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "create_return"},
         "startTimeUnixNano": 2,
         "events": [{"name": "gen_ai.tool.message", "attributes": {"content": '{"order_id": "ORD-1002", "reason": "x"}'}}]},
    ]
    r = handler.handler(event(SPANS + calls), None)
    assert r["label"] == "VIOLATION" and "45 days > 30-day apparel" in r["explanation"], r


def test_missing_spans_is_evaluator_error_not_a_score():
    r = handler.handler({"evaluationInput": {}}, None)
    assert r["errorCode"] == "MissingSessionSpans" and "value" not in r
    r = handler.handler(event([]), None)
    assert r["errorCode"] == "NoAgentAnswer"


def test_luhn_filters_random_digits():
    assert handler.card_numbers("order 1234567890123 shipped") == []
    assert handler.card_numbers("5500-0000-0000-0004") == ["0004"]


def _with_answer(text):
    spans = copy.deepcopy(SPANS)
    agent = next(s for s in spans if s["attributes"]["gen_ai.operation.name"] == "invoke_agent")
    next(e for e in agent["events"] if e["name"] == "gen_ai.choice")["attributes"]["message"] = text
    return spans


def test_card_number_inside_a_json_answer_is_found():
    # Continuum finding: a JSON-array answer used to be reduced to its "text" keys only
    r = handler.handler(event(_with_answer(json.dumps([{"card": "4111111111111111"}, "ok"]))), None)
    assert r["label"] == "VIOLATION", r


def test_card_number_with_other_separators_or_padding_is_found():
    for text in ["4111.1111.1111.1111", "4111_1111_1111_1111", "4111\u00a01111\u00a01111\u00a01111",
                 "4111\u20111111\u20111111\u20111111", "41111111111111119", "\uff14\uff11\uff11\uff11" + "111111111111"]:
        assert handler.card_numbers(text), text


def test_ordinary_ids_and_dates_are_not_card_numbers():
    for text in ["ORD-1001 delivered 2026-11-08, RMA-5001", "call 1-800-555-0199", "order 1234567890123 shipped"]:
        assert handler.card_numbers(text) == [], text


def test_r2_runs_without_answer_text_and_a_failing_rule_keeps_violations(monkeypatch):
    calls = [s for s in SPANS if s["attributes"].get("gen_ai.operation.name") != "invoke_agent"]
    calls = calls + [
        {"attributes": {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "get_order"}, "startTimeUnixNano": 1,
         "events": [{"name": "gen_ai.choice", "attributes": {"message": json.dumps([{"text": json.dumps(
             {"order_id": "ORD-1002", "category": "apparel", "days_since_delivery": 45})}])}}]},
        {"attributes": {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "create_return"}, "startTimeUnixNano": 2,
         "events": [{"name": "gen_ai.tool.message", "attributes": {"content": '{"order_id": "ORD-1002"}'}}]}]
    r = handler.handler(event(calls), None)
    assert r["label"] == "VIOLATION" and "R2" in r["explanation"], r

    def boom(spans):
        raise RuntimeError("broken rule")
    monkeypatch.setattr(handler, "RULES", (boom, handler.r2_return_window))
    r = handler.handler(event(SPANS + calls[-2:]), None)
    assert r["label"] == "VIOLATION" and "broken rule" in r["explanation"], r
    monkeypatch.setattr(handler, "RULES", (boom,))
    r = handler.handler(event(SPANS), None)
    assert r.get("errorCode") == "EvaluatorFailed" and "value" not in r, r
