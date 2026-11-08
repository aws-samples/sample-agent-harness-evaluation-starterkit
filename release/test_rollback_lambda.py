"""Unit tests for release/rollback_lambda.py with a fake EventBridge event and stub clients (no AWS calls)."""

import json

import pytest

import rollback_lambda as rb


def alarm_event(name, state="ALARM"):
    return {"version": "0", "source": "aws.cloudwatch", "detail-type": "CloudWatch Alarm State Change",
            "account": "123456789012", "region": "us-east-1",
            "resources": [f"arn:aws:cloudwatch:us-east-1:123456789012:alarm:{name}"],
            "detail": {"alarmName": name, "state": {"value": state, "reason": "Threshold Crossed"},
                       "previousState": {"value": "OK"}}}


class _NotFound(Exception):
    pass


class FakeSSM:
    """SSM with /evaldeck/release/current + /previous; legacy shorthand {"current": "7", "previous": "5"}."""
    class exceptions:
        ParameterNotFound = _NotFound

    def __init__(self, value=None):
        self.store, self.put = {}, None
        if value is not None:
            self.store["/evaldeck/release/current"] = {"runtimeVersion": value["current"], "gate_key": "k7",
                                                       "rollbacks": value.get("rollbacks", [])}
            self.store["/evaldeck/release/previous"] = value.get("previous") and {"runtimeVersion": value["previous"],
                                                                                   "gate_key": "k5"}

    def get_parameter(self, Name):
        if Name not in self.store:
            raise _NotFound(Name)
        return {"Parameter": {"Value": json.dumps(self.store[Name])}}

    def put_parameter(self, **kw):
        self.store[kw["Name"]] = json.loads(kw["Value"])
        self.put = kw


class FakeControl:
    def __init__(self):
        self.calls = []

    def update_agent_runtime_endpoint(self, **kw):
        self.calls.append(kw)
        return {"liveVersion": "5", "targetVersion": kw["agentRuntimeVersion"]}


class FakeData:
    def __init__(self, status="RUNNING"):
        self.status, self.updates = status, []

    def get_ab_test(self, abTestId):
        return {"executionStatus": self.status}

    def update_ab_test(self, **kw):
        self.updates.append(kw)


class FakeCW:
    def __init__(self, state="ALARM"):
        self.state = state

    def describe_alarms(self, AlarmNames):
        return {"MetricAlarms": [{"AlarmName": AlarmNames[0], "StateValue": self.state}] if self.state else []}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("EXPECTED_ACCOUNT", "123456789012")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.delenv("ALLOWED_ALARMS", raising=False)
    monkeypatch.setenv("AGENT_RUNTIME_ID", "evaldeck_default_ShopAgent-abc")
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.delenv("AB_TEST_ID", raising=False)


def test_redline_alarm_rolls_prod_back_once():
    ctl, ssm = FakeControl(), FakeSSM({"current": "7", "previous": "5", "rollbacks": []})
    d = rb.handler(alarm_event("evaldeck-shopagent-redline"), None, (ctl, FakeData(), ssm))
    assert d["decision"] == "rollback" and d["to"] == "5"
    assert ctl.calls == [{"agentRuntimeId": "evaldeck_default_ShopAgent-abc", "endpointName": "PROD",
                          "agentRuntimeVersion": "5"}]
    rec = ssm.store["/evaldeck/release/current"]
    assert rec["runtimeVersion"] == "5" and rec["rollbacks"][0]["from"] == "7" and rec["rolledBackFrom"] == "k7"
    assert ssm.store["/evaldeck/release/previous"] is None
    # the same release cannot be rolled back twice
    again = rb.decide("evaldeck-shopagent-redline", {"current": "7", "previous": "5", "rollbacks": rec["rollbacks"]})
    assert again["decision"] == "already-rolled-back"


def test_eval_silent_only_notifies():
    ctl = FakeControl()
    d = rb.handler(alarm_event("evaldeck-shopagent-eval-silent"), None, (ctl, FakeData(), FakeSSM({"current": "7", "previous": "5"})))
    assert d["decision"] == "notify-only" and ctl.calls == []


def test_running_ab_test_is_paused_not_rolled_back(monkeypatch):
    monkeypatch.setenv("AB_TEST_ID", "ab-123")
    ctl, dp = FakeControl(), FakeData("RUNNING")
    d = rb.handler(alarm_event("evaldeck-shopagent-goal-score"), None, (ctl, dp, FakeSSM({"current": "7", "previous": "5"})))
    assert d["decision"] == "pause-ab-test" and dp.updates == [{"abTestId": "ab-123", "executionStatus": "PAUSED"}]
    assert ctl.calls == []


def test_first_release_and_foreign_alarms_do_nothing():
    ctl = FakeControl()
    assert rb.handler(alarm_event("evaldeck-shopagent-redline"), None, (ctl, FakeData(), FakeSSM(None)))["decision"] == "no-previous-release"
    assert rb.handler(alarm_event("someone-elses-alarm"), None, (ctl, FakeData(), FakeSSM(None)))["decision"] == "ignored"
    assert rb.handler(alarm_event("evaldeck-shopagent-redline", "OK"), None, (ctl, FakeData(), FakeSSM(None)))["decision"] == "ignored"
    assert ctl.calls == []


def test_dry_run_changes_nothing(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "true")
    ctl, ssm = FakeControl(), FakeSSM({"current": "7", "previous": "5"})
    d = rb.handler(alarm_event("evaldeck-shopagent-redline"), None, (ctl, FakeData(), ssm))
    assert d["decision"] == "rollback" and d["dry_run"] and ctl.calls == [] and ssm.put is None


class FakeControlWithRules(FakeControl):
    def __init__(self):
        super().__init__()
        self.rule_updates = []

    def list_gateway_rules(self, gatewayIdentifier):
        return {"gatewayRules": [{"ruleId": "r-1", "priority": 100, "description": "evaldeck-ship: production"}]}

    def update_gateway_rule(self, **kw):
        self.rule_updates.append(kw)


def test_rollback_also_moves_the_bundle_rule(monkeypatch):
    monkeypatch.setenv("GATEWAY_ID", "gw-1")
    ctl, ssm = FakeControlWithRules(), FakeSSM()
    ssm.store = {"/evaldeck/release/current": {"runtimeVersion": "7", "bundleArn": "arn:b", "bundleVersion": "v-new",
                                               "gate_key": "k7", "rollbacks": []},
                 "/evaldeck/release/previous": {"runtimeVersion": "5", "bundleArn": "arn:b", "bundleVersion": "v-old",
                                                "gate_key": "k5"}}
    d = rb.handler(alarm_event("evaldeck-shopagent-redline"), None, (ctl, FakeData(), ssm))
    assert d["bundle"] == {"from": "v-new", "to": "v-old"}
    assert ctl.rule_updates[0]["actions"][0]["configurationBundle"]["staticOverride"]["bundleVersion"] == "v-old"
    assert ssm.store["/evaldeck/release/current"]["bundleVersion"] == "v-old"


def test_forged_or_stale_triggers_are_ignored(monkeypatch):
    ssm = lambda: FakeSSM({"current": "7", "previous": "5"})  # noqa: E731
    cases = [
        ({**alarm_event("evaldeck-shopagent-redline"), "source": "my.app"}, FakeCW(), "not a CloudWatch"),
        ({**alarm_event("evaldeck-shopagent-redline"), "account": "999999999999"}, FakeCW(), "account"),
        ({**alarm_event("evaldeck-shopagent-redline"),
          "resources": ["arn:aws:cloudwatch:us-east-1:123456789012:alarm:other"]}, FakeCW(), "alarm ARN"),
        (alarm_event("evaldeck-shopagent-redline"), FakeCW("OK"), "DescribeAlarms"),     # direct invoke, alarm is OK
        (alarm_event("evaldeck-shopagent-redline"), FakeCW(None), "missing"),
    ]
    for ev, cw, why in cases:
        ctl = FakeControl()
        d = rb.handler(ev, None, (ctl, FakeData(), ssm(), cw))
        assert d["decision"] == "ignored" and why in d["why"] and ctl.calls == [], (why, d)
    monkeypatch.setenv("ALLOWED_ALARMS", "evaldeck-shopagent-goal-score")
    d = rb.handler(alarm_event("evaldeck-shopagent-redline"), None, (FakeControl(), FakeData(), ssm(), FakeCW()))
    assert d["decision"] == "ignored" and "ALLOWED_ALARMS" in d["why"]


def test_record_keeps_the_served_bundle_when_the_ship_rule_was_not_moved():
    ctl, ssm = FakeControl(), FakeSSM()                     # GATEWAY_ID not set
    ssm.store = {"/evaldeck/release/current": {"runtimeVersion": "7", "bundleArn": "arn:b", "bundleVersion": "v-new",
                                               "gate_key": "k7", "rollbacks": []},
                 "/evaldeck/release/previous": {"runtimeVersion": "5", "bundleArn": "arn:b", "bundleVersion": "v-old",
                                                "gate_key": "k5"}}
    d = rb.handler(alarm_event("evaldeck-shopagent-redline"), None, (ctl, FakeData(), ssm, FakeCW()))
    rec = ssm.store["/evaldeck/release/current"]
    assert rec["runtimeVersion"] == "5" and rec["bundleVersion"] == "v-new" and rec["bundleNotRolledBack"]
    assert d["bundle_warning"]
