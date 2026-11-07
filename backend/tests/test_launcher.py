import pytest

from harness.cli import format_event
from harness.launcher import (
    LauncherError,
    RunInfo,
    build_child_env,
    exit_code_for,
    resolve_operator_token,
)

INFO = RunInfo("abc123", "tok-en_1", "127.0.0.1", 8080, "/ca/mitm.pem")


# --- child environment ---------------------------------------------------------------

def test_all_traffic_is_routed_through_the_gateway_in_both_cases():
    env = build_child_env({}, INFO)
    url = "http://abc123:tok-en_1@127.0.0.1:8080"
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        assert env[name] == url


def test_inherited_proxy_settings_are_replaced_and_nothing_is_exempt():
    base = {"HTTPS_PROXY": "http://corp:3128", "no_proxy": "localhost,127.0.0.1", "NO_PROXY": "internal"}
    env = build_child_env(base, INFO)
    assert "corp" not in env["HTTPS_PROXY"]
    assert env["NO_PROXY"] == "" and env["no_proxy"] == ""


def test_gateway_ca_is_trusted_by_common_tools():
    env = build_child_env({}, INFO)
    for name in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
                 "AWS_CA_BUNDLE", "GIT_SSL_CAINFO"):
        assert env[name] == "/ca/mitm.pem"


def test_operator_credentials_are_never_given_to_the_agent():
    base = {"HARNESS_OPERATOR_TOKEN": "secret", "HARNESS_HOME": "/h", "HARNESS_API": "http://x"}
    env = build_child_env(base, INFO)
    assert not any(k.startswith("HARNESS_") and k != "HARNESS_RUN_ID" for k in env)
    assert "secret" not in "".join(env.values())


def test_other_variables_including_aws_credentials_pass_through():
    base = {"PATH": "/bin", "AWS_ACCESS_KEY_ID": "AKIAX", "AWS_SESSION_TOKEN": "t", "FOO": "bar"}
    env = build_child_env(base, INFO)
    assert env["PATH"] == "/bin" and env["AWS_ACCESS_KEY_ID"] == "AKIAX"
    assert env["AWS_SESSION_TOKEN"] == "t" and env["FOO"] == "bar"
    assert env["HARNESS_RUN_ID"] == "abc123"


def test_base_environment_is_not_mutated():
    base = {"HTTPS_PROXY": "x", "HARNESS_OPERATOR_TOKEN": "s"}
    build_child_env(base, INFO)
    assert base == {"HTTPS_PROXY": "x", "HARNESS_OPERATOR_TOKEN": "s"}


def test_missing_ca_is_an_error():
    with pytest.raises(LauncherError):
        build_child_env({}, RunInfo("r", "t", "h", 1, None))


# --- exit codes & tokens -----------------------------------------------------------------

@pytest.mark.parametrize("rc,code", [(0, 0), (3, 3), (-15, 143), (-9, 137), (-2, 130)])
def test_exit_code_follows_shell_convention(rc, code):
    assert exit_code_for(rc) == code


def test_token_from_env_wins(tmp_path):
    (tmp_path / "operator_token").write_text("from-file")
    assert resolve_operator_token({"HARNESS_OPERATOR_TOKEN": " from-env \n"}, tmp_path) == "from-env"


def test_token_from_file_is_stripped(tmp_path):
    (tmp_path / "operator_token").write_text("from-file\n")
    assert resolve_operator_token({}, tmp_path) == "from-file"


def test_missing_token_explains_how_to_fix(tmp_path):
    with pytest.raises(LauncherError) as e:
        resolve_operator_token({}, tmp_path)
    assert "harness serve" in str(e.value)


# --- event formatting ----------------------------------------------------------------------

def ev(**kw):
    return {"ts": "2026-11-07T12:00:01.000000Z", "run_id": "r1", "kind": "request", **kw}


def test_allowed_line():
    line = format_event(ev(decision="allowed", method="GET", host="api.github.com", path="/zen",
                           rule_id="allow-github-read", status_code=200), color=False)
    assert "ALLOW" in line and "GET" in line and "api.github.com/zen" in line
    assert "200" in line and "allow-github-read" in line and "[r1]" in line


def test_blocked_line_shows_the_reason():
    line = format_event(ev(decision="blocked", method="POST", host="pastebin.com", path="/x",
                           rule_id="block-paste-sites", status_code=403), color=False)
    assert "BLOCK" in line and "(block-paste-sites)" in line


def test_blocked_without_rule_falls_back_to_reason():
    line = format_event(ev(decision="blocked", host="h", path="/", reason="rate limit exceeded"), color=False)
    assert "(rate limit exceeded)" in line


def test_allowed_request_is_shown_once_when_its_response_arrives():
    sent = ev(decision="allowed", method="GET", host="api.github.com", path="/zen")
    assert format_event(sent, color=False) is None
    assert "200" in format_event({**sent, "status_code": 200}, color=False)
    assert "502" in format_event({**sent, "status_code": 502}, color=False)


def test_pending_and_tool_and_truncation():
    line = format_event(ev(method="POST", host="h", path="/" + "a" * 200, tool="delete_repo"), color=False)
    assert "..." in line and "tool=delete_repo" in line and len(line) < 200


def test_lifecycle_lines():
    assert "RUN STOPPED" in format_event(ev(kind="run_stopped", reason="policy: rule 'x'"), color=False)
    assert "RUN STARTED" in format_event(ev(kind="run_started", reason="python a.py"), color=False)
    assert "POLICY" in format_event(ev(kind="policy_reloaded", reason="policy v2 active"), color=False)


def test_color_adds_ansi_and_plain_does_not():
    e = ev(decision="blocked", host="h", path="/")
    assert "\x1b[" in format_event(e, color=True)
    assert "\x1b[" not in format_event(e, color=False)


def test_bad_timestamp_does_not_crash():
    assert "--:--:--" in format_event({"kind": "request", "ts": "garbage", "run_id": "r"}, color=False)
