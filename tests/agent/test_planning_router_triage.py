"""A1 (openhuman triage borrow): a flag-gated grey-zone classifier for the
planning router. Covers the tolerant parser (fenced JSON, prose-around,
trailing commas, wrong-case, last-block-wins), the direct|plan mapping, the
Deferred / exhausted semantics, and the flag-OFF byte-identical invariant.
"""

from agent import planning_router as pr


# --- tolerant parser (decision.rs contract) ---------------------------------
class TestParseTriageDecision:
    def test_fenced_json(self):
        raw = 'ok\n```json\n{"action": "drop", "reason": "noise"}\n```'
        d = pr.parse_triage_decision(raw)
        assert d is not None and d.action == "drop" and d.reason == "noise"

    def test_bare_object_in_prose(self):
        d = pr.parse_triage_decision('sure {"action": "React", "reason": "one step"} ok')
        assert d is not None and d.action == "react"

    def test_wrong_case_action(self):
        d = pr.parse_triage_decision('{"action": "ACKNOWLEDGE", "reason": "note"}')
        assert d is not None and d.action == "acknowledge"

    def test_trailing_commas_repaired(self):
        d = pr.parse_triage_decision('{"action":"escalate","reason":"multi",}')
        assert d is not None and d.action == "escalate"

    def test_last_block_wins(self):
        raw = '{"action": "drop"}\nrevised: {"action": "react", "reason": "x"}'
        assert pr.parse_triage_decision(raw).action == "react"

    def test_garbage_returns_none(self):
        assert pr.parse_triage_decision("no json here at all") is None
        assert pr.parse_triage_decision("") is None
        assert pr.parse_triage_decision(None) is None

    def test_unknown_action_returns_none(self):
        assert pr.parse_triage_decision('{"action": "explode", "reason": "?"}') is None


# --- classify_route: ON decisions map to direct|plan + triage:<action> ------
def _decider(action):
    return lambda text: f"decided:{action}"


class TestClassifyRouteOn:
    def test_drop_is_direct(self):
        route, reason, _ = pr.classify_route("разберись с этим", triage_classifier=_decider("drop"))
        assert (route, reason) == ("direct", "triage:drop")

    def test_acknowledge_is_direct(self):
        route, reason, _ = pr.classify_route("разберись с этим", triage_classifier=_decider("acknowledge"))
        assert (route, reason) == ("direct", "triage:acknowledge")

    def test_react_is_plan(self):
        route, reason, _ = pr.classify_route("разберись с этим", triage_classifier=_decider("react"))
        assert (route, reason) == ("plan", "triage:react")

    def test_escalate_is_plan(self):
        route, reason, _ = pr.classify_route("разберись с этим", triage_classifier=_decider("escalate"))
        assert (route, reason) == ("plan", "triage:escalate")

    def test_failure_defers_not_admit(self):
        def boom(text):
            raise RuntimeError("endpoint down")

        calls = []
        route, reason, _ = pr.classify_route(
            "разберись с этим", triage_classifier=boom, on_defer=lambda t: calls.append(t))
        # no classifier exception may become plan; it defers (direct) and schedules a retry
        assert reason == "triage:deferred"
        assert route == "direct"
        assert calls == ["разберись с этим"]

    def test_exhausted_falls_through_to_default(self):
        route, reason, _ = pr.classify_route("разберись с этим", triage_classifier=lambda t: "exhausted")
        assert (route, reason) == ("plan", "grey_zone_default")

    def test_deterministic_rules_still_precede_triage(self):
        # an explicit plan request is decided by the deterministic pass, never triaged
        seen = []
        route, reason, _ = pr.classify_route(
            "составь план", triage_classifier=lambda t: seen.append(1) or "decided:drop")
        assert (route, reason) == ("plan", "explicit_plan_request")
        assert not seen  # the classifier was never consulted


# --- classify_route: OFF is byte-identical to the deterministic router ------
class TestClassifyRouteOff:
    def test_grey_defaults_to_plan(self):
        assert pr.classify_route("разберись с этим") == ("plan", "grey_zone_default", 0.5)

    def test_chatter_is_direct(self):
        assert pr.classify_route("ок") == ("direct", "chatter_ack", 0.8)

    def test_classifier_kwarg_absent_by_default(self):
        # calling with no triage args is exactly the legacy behaviour
        assert pr.classify_route("кину файл") == ("direct", "file_handover", 0.8)


# --- build_triage_classifier: tiered cloud->local + exhaustion --------------
class TestBuildTriageClassifier:
    def test_cloud_success(self):
        f = pr.build_triage_classifier(cloud_call=lambda p: '{"action":"react","reason":"x"}')
        assert f("м") == "decided:react"

    def test_cloud_fails_falls_to_local(self):
        f = pr.build_triage_classifier(
            cloud_call=lambda p: (_ for _ in ()).throw(RuntimeError("429")),
            local_call=lambda p: '{"action":"escalate","reason":"y"}')
        assert f("м") == "decided:escalate"

    def test_all_fail_defers_then_exhausts(self):
        f = pr.build_triage_classifier(cloud_call=lambda p: None, local_call=None, defer_budget=2)
        assert f("м") == pr.TRIAGE_DEFERRED
        assert f("м") == pr.TRIAGE_DEFERRED
        assert f("м") == pr.TRIAGE_EXHAUSTED

    def test_one_retry_on_cloud_then_local(self):
        hits = {"n": 0}

        def cloud(p):
            hits["n"] += 1
            return ""  # transient/empty reply: the tiered loop gives one retry

        f = pr.build_triage_classifier(cloud_call=cloud, local_call=lambda p: '{"action":"drop"}')
        assert f("м") == "decided:drop"
        assert hits["n"] == 2  # initial + one retry, then the local arm
