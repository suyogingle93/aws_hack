import json

from harness.mcp import MAX_BODY_BYTES, extract_tool_calls, tool_names


def rpc(name, args=None, id=1):
    return {"jsonrpc": "2.0", "id": id, "method": "tools/call", "params": {"name": name, "arguments": args or {}}}


def test_single_tool_call():
    calls = extract_tool_calls(json.dumps(rpc("delete_repo", {"repo": "x"})))
    assert [(c.name, c.arguments) for c in calls] == [("delete_repo", {"repo": "x"})]


def test_batch_returns_all_tool_calls():
    body = json.dumps([rpc("a"), {"jsonrpc": "2.0", "method": "tools/list", "id": 2}, rpc("b", id=3)])
    assert tool_names(body) == ("a", "b")


def test_accepts_bytes():
    assert tool_names(json.dumps(rpc("x")).encode()) == ("x",)


def test_non_tool_methods_ignored():
    assert tool_names(json.dumps({"jsonrpc": "2.0", "method": "initialize", "id": 1})) == ()


def test_garbage_and_empty_bodies():
    for body in (None, b"", "not json", b"\xff\xfe", "123", "null", "[1, 2]"):
        assert extract_tool_calls(body) == []


def test_malformed_params_ignored():
    bad = [
        {"method": "tools/call"},
        {"method": "tools/call", "params": "x"},
        {"method": "tools/call", "params": {"name": 5}},
        {"method": "tools/call", "params": {"name": ""}},
    ]
    assert extract_tool_calls(json.dumps(bad)) == []


def test_non_dict_arguments_become_empty():
    body = json.dumps({"method": "tools/call", "params": {"name": "t", "arguments": [1]}})
    assert extract_tool_calls(body)[0].arguments == {}


def test_oversized_body_skipped():
    assert extract_tool_calls(b" " * (MAX_BODY_BYTES + 1)) == []
