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
