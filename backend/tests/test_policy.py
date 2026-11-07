from pathlib import Path

import pytest

from harness.policy import (
    Policy,
    PolicyEngine,
    PolicyError,
    RequestContext,
    Rule,
    evaluate_policy,
    load_policy_file,
    parse_policy,
)

DEMO = Path(__file__).resolve().parents[1] / "policies" / "demo.yaml"


def ctx(host="example.com", path="/", method="GET", tools=(), run_id="r1"):
    return RequestContext(run_id=run_id, method=method, host=host, path=path, tools=tuple(tools))


def policy(rules, default="deny", **kw):
    return Policy(default=default, rules=rules, **kw)


# --- matching -------------------------------------------------------------

def test_default_applies_when_nothing_matches():
    assert evaluate_policy(policy([], default="deny"), ctx()).action == "deny"
    assert evaluate_policy(policy([], default="allow"), ctx()).action == "allow"


def test_host_glob_is_case_insensitive():
    p = policy([Rule(id="a", action="allow", host="*.Example.com")])
    assert evaluate_policy(p, ctx(host="api.example.COM")).allowed


def test_method_filter():
    p = policy([Rule(id="a", action="allow", host="x.com", methods=["get"])])
    assert evaluate_policy(p, ctx(host="x.com", method="GET")).allowed
    assert not evaluate_policy(p, ctx(host="x.com", method="POST")).allowed


def test_rule_requires_all_specified_fields():
    p = policy([Rule(id="a", action="allow", host="x.com", path="/ok/*")])
    assert evaluate_policy(p, ctx(host="x.com", path="/ok/1")).allowed
    assert not evaluate_policy(p, ctx(host="x.com", path="/no/1")).allowed
    assert not evaluate_policy(p, ctx(host="y.com", path="/ok/1")).allowed


def test_path_ignores_query_and_decodes_percent_encoding():
    p = policy([Rule(id="a", action="allow", path="/model/us.meta.llama4:0/*")])
    assert evaluate_policy(p, ctx(path="/model/us.meta.llama4%3A0/converse?x=1")).allowed


def test_mcp_tool_rule_needs_a_tool():
    p = policy([Rule(id="d", action="deny", mcp_tool="delete_*")], default="allow")
    assert evaluate_policy(p, ctx()).allowed  # no tool in request
    assert not evaluate_policy(p, ctx(tools=["delete_repo"])).allowed


def test_batch_with_one_denied_tool_is_denied():
    p = policy([Rule(id="d", action="deny", mcp_tool="delete_*")], default="allow")
    assert not evaluate_policy(p, ctx(tools=["list_files", "delete_repo"])).allowed


# --- precedence -----------------------------------------------------------

@pytest.mark.parametrize("order", [("allow", "deny"), ("deny", "allow")])
def test_deny_beats_allow_regardless_of_order(order):
    rules = [Rule(id=f"r-{a}", action=a, host="x.com") for a in order]
    d = evaluate_policy(policy(rules, default="allow"), ctx(host="x.com"))
    assert d.action == "deny" and d.rule_id == "r-deny"


def test_decision_reports_rule_and_terminate():
    p = policy([Rule(id="bad", action="deny", host="x.com", terminate_on_violation=True)])
    d = evaluate_policy(p, ctx(host="x.com"))
    assert d.rule_id == "bad" and d.terminate and "bad" in d.reason


# --- validation -----------------------------------------------------------

def test_rule_needs_a_matcher():
    with pytest.raises(ValueError):
        Rule(id="empty", action="allow")


def test_terminate_only_on_deny():
    with pytest.raises(ValueError):
        Rule(id="x", action="allow", host="a", terminate_on_violation=True)


def test_duplicate_ids_rejected():
    with pytest.raises(PolicyError):
        parse_policy({"rules": [{"id": "a", "action": "allow", "host": "x"}, {"id": "a", "action": "deny", "host": "y"}]})


def test_unknown_fields_rejected():
    with pytest.raises(PolicyError):
        parse_policy({"default": "deny", "bogus": 1})


def test_non_mapping_rejected():
    with pytest.raises(PolicyError):
        parse_policy(["not", "a", "mapping"])


# --- rate limiting --------------------------------------------------------

class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_rate_limit_blocks_then_recovers():
    clock = FakeClock()
    p = parse_policy({"default": "allow", "rate_limit": {"requests_per_minute": 2}})
    engine = PolicyEngine(policy=p, clock=clock)
    assert engine.evaluate(ctx()).allowed
    assert engine.evaluate(ctx()).allowed
    d = engine.evaluate(ctx())
    assert not d.allowed and "rate limit" in d.reason
    clock.t = 61
    assert engine.evaluate(ctx()).allowed


def test_rate_limit_is_per_run():
    p = parse_policy({"default": "allow", "rate_limit": {"requests_per_minute": 1}})
    engine = PolicyEngine(policy=p, clock=FakeClock())
    assert engine.evaluate(ctx(run_id="a")).allowed
    assert engine.evaluate(ctx(run_id="b")).allowed
    assert not engine.evaluate(ctx(run_id="a")).allowed


# --- reload ---------------------------------------------------------------

def test_reload_applies_new_policy(tmp_path):
    f = tmp_path / "p.yaml"
    f.write_text("default: deny\n")
    engine = PolicyEngine.from_file(f)
    assert not engine.evaluate(ctx()).allowed
    f.write_text("default: allow\n")
    assert engine.reload() == 2
    assert engine.evaluate(ctx()).allowed


def test_invalid_reload_keeps_old_policy(tmp_path):
    f = tmp_path / "p.yaml"
    f.write_text("default: allow\n")
    engine = PolicyEngine.from_file(f)
    f.write_text("default: [unclosed")
    with pytest.raises(PolicyError):
        engine.reload()
    assert engine.version == 1
    assert engine.evaluate(ctx()).allowed


def test_reload_without_source_fails():
    with pytest.raises(PolicyError):
        PolicyEngine(policy=Policy()).reload()


# --- the demo policy ------------------------------------------------------

@pytest.fixture
def demo():
    return PolicyEngine(policy=load_policy_file(DEMO))


def test_demo_allows_approved_bedrock_model(demo):
    d = demo.evaluate(ctx("bedrock-runtime.us-east-1.amazonaws.com",
                          "/model/us.meta.llama4-scout-17b-instruct-v1%3A0/converse", "POST"))
    assert d.allowed and d.rule_id == "allow-bedrock-approved-model"


def test_demo_blocks_other_bedrock_model(demo):
    d = demo.evaluate(ctx("bedrock-runtime.us-east-1.amazonaws.com",
                          "/model/amazon.titan-text-lite-v1/converse", "POST"))
    assert not d.allowed and d.rule_id is None and "default" in d.reason


def test_demo_allows_github_get_but_not_post(demo):
    assert demo.evaluate(ctx("api.github.com", "/zen", "GET")).allowed
    assert not demo.evaluate(ctx("api.github.com", "/repos/x/y", "POST")).allowed


def test_demo_pastebin_terminates_run(demo):
    d = demo.evaluate(ctx("pastebin.com", "/api/api_post.php", "POST"))
    assert not d.allowed and d.terminate and d.rule_id == "block-paste-sites"


def test_demo_blocks_destructive_tool_even_on_allowed_host(demo):
    d = demo.evaluate(ctx("api.github.com", "/mcp", "POST", tools=["delete_repo"]))
    assert not d.allowed and d.rule_id == "block-destructive-tools"
