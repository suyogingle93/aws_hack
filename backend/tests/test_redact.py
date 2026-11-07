from harness.redact import REDACTED, redact_headers, redact_path, redact_text


def test_sensitive_headers_are_masked_case_insensitively():
    out = redact_headers({"X-Api-Key": "abc", "COOKIE": "s=1", "Accept": "json"})
    assert out["X-Api-Key"] == REDACTED
    assert out["COOKIE"] == REDACTED
    assert out["Accept"] == "json"


def test_authorization_keeps_scheme_only():
    out = redact_headers({"Authorization": "Bearer abcdefghijkl"})
    assert out["Authorization"] == f"Bearer {REDACTED}"


def test_sigv4_authorization_signature_is_removed():
    value = (
        "AWS4-HMAC-SHA256 Credential=ASIAABCDEFGHIJKLMNOP/20261107/us-east-1/bedrock/aws4_request, "
        "SignedHeaders=host;x-amz-date, Signature=0123456789abcdef0123456789abcdef"
    )
    out = redact_headers({"Authorization": value})["Authorization"]
    assert "0123456789abcdef" not in out and "ASIAABCDEFGHIJKLMNOP" not in out


def test_aws_session_token_header_masked():
    assert redact_headers({"X-Amz-Security-Token": "FwoGZX..."})["X-Amz-Security-Token"] == REDACTED


def test_proxy_authorization_masked():
    out = redact_headers({"Proxy-Authorization": "Basic cnVuOnRva2Vu"})
    assert "cnVuOnRva2Vu" not in out["Proxy-Authorization"]


def test_token_patterns_in_other_headers_are_masked():
    out = redact_headers({"X-Debug": "key AKIAABCDEFGHIJKLMNOP here"})
    assert "AKIAABCDEFGHIJKLMNOP" not in out["X-Debug"]


def test_redact_text_github_and_sk_tokens():
    s = redact_text("ghp_" + "a" * 30 + " and sk-" + "b" * 25)
    assert "ghp_" not in s and "sk-" not in s


def test_path_query_secrets_masked_but_path_kept():
    out = redact_path("/v1/items?token=secret123&page=2&X-Amz-Signature=deadbeef")
    assert out.startswith("/v1/items?")
    assert "secret123" not in out and "deadbeef" not in out
    assert "page=2" in out


def test_path_without_query_unchanged():
    assert redact_path("/model/x/converse") == "/model/x/converse"


def test_input_headers_not_mutated():
    headers = {"Authorization": "Bearer abcdefghijkl"}
    redact_headers(headers)
    assert headers["Authorization"] == "Bearer abcdefghijkl"
