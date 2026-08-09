"""Dependency-free tests for LLM routing. Run: `python3 test_llm.py`.

Stubs `requests` so the LiteLLM path is exercised without network, and a fake
genai client for the fallback path.
"""

from __future__ import annotations

import os
import sys
import types

# Stub `requests` before importing llm.
_captured: dict[str, object] = {}


class _Resp:
    def __init__(self, payload, status_code=200, text=""):
        self._payload = payload
        self.status_code = status_code
        self.text = text

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _fake_post(url, headers=None, json=None, timeout=None):
    _captured["url"] = url
    _captured["headers"] = headers
    _captured["json"] = json
    _captured["timeout"] = timeout
    return _Resp({"choices": [{"message": {"content": '{"ok": true}'}}]})


_req = types.ModuleType("requests")
_req.post = _fake_post
sys.modules["requests"] = _req

import llm  # noqa: E402


def _clear_env():
    for k in ("LITELLM_BASE_URL", "LITELLM_API_KEY", "LLM_MODEL", "GEMINI_MODEL",
              "LLM_SESSION_ID", "GITHUB_REPOSITORY"):
        os.environ.pop(k, None)


def test_session_id_derivation() -> None:
    _clear_env()
    os.environ["GITHUB_REPOSITORY"] = "AriOliv/mike"
    assert llm.session_id("review", ref=12) == "AriOliv/mike#pr-12"
    assert llm.session_id("issue", ref=7) == "AriOliv/mike#issue-7"
    assert llm.session_id("scan") == "AriOliv/mike#scan"
    os.environ["LLM_SESSION_ID"] = "custom-123"
    assert llm.session_id("review", ref=99) == "custom-123"
    os.environ.pop("LLM_SESSION_ID")


def test_proxy_routing_sends_user_and_tags() -> None:
    _clear_env()
    os.environ["LITELLM_BASE_URL"] = "https://litellm.example.com/"
    os.environ["LITELLM_API_KEY"] = "sk-proxy"
    os.environ["LLM_MODEL"] = "gemini-3.6-flash"
    assert llm.proxy_enabled()
    out = llm.generate_json(prompt="P", system_instruction="S", kind="review",
                            session="AriOliv/mike#pr-12")
    assert out == '{"ok": true}'
    assert _captured["url"] == "https://litellm.example.com/chat/completions"
    h = _captured["headers"]
    assert h["Authorization"] == "Bearer sk-proxy"
    assert h["x-litellm-tags"] == "gh-pr-reviewer,review"
    body = _captured["json"]
    assert body["user"] == "AriOliv/mike#pr-12"
    assert body["model"] == "gemini-3.6-flash"
    assert body["messages"][0]["role"] == "system" and body["messages"][1]["content"] == "P"
    assert body["response_format"] == {"type": "json_object"}


def test_fallback_uses_genai_client_when_no_proxy() -> None:
    _clear_env()

    class FakeModels:
        def generate_content(self, model, contents, config):
            _captured["fallback_model"] = model
            return types.SimpleNamespace(text='{"fell":"back"}')

    class FakeClient:
        models = FakeModels()

    assert not llm.proxy_enabled()
    out = llm.generate_json(prompt="P", system_instruction="S", kind="scan",
                            session="x", genai_client=FakeClient(), model="gemini-3.6-flash")
    assert out == '{"fell":"back"}'
    assert _captured["fallback_model"] == "gemini-3.6-flash"


def test_proxy_http_error_surfaces_body() -> None:
    _clear_env()
    os.environ["LITELLM_BASE_URL"] = "https://litellm.example.com"
    os.environ["LLM_MODEL"] = "m"
    _req.post = lambda *a, **k: _Resp(
        None, status_code=400, text='{"error":{"message":"model not found: m"}}'
    )
    try:
        llm.generate_json(prompt="P", system_instruction="S", kind="review", session="x")
        raise AssertionError("expected RuntimeError")
    except RuntimeError as e:
        assert "HTTP 400" in str(e) and "model not found" in str(e)
    _req.post = _fake_post  # restore


def test_proxy_bad_shape_surfaces_payload() -> None:
    _clear_env()
    os.environ["LITELLM_BASE_URL"] = "https://litellm.example.com"
    _req.post = lambda *a, **k: _Resp({"unexpected": 1})
    try:
        llm.generate_json(prompt="P", system_instruction="S", kind="scan", session="x")
        raise AssertionError("expected RuntimeError")
    except RuntimeError as e:
        assert "missing choices" in str(e)
    _req.post = _fake_post  # restore


def test_fallback_without_client_raises() -> None:
    _clear_env()
    try:
        llm.generate_json(prompt="P", system_instruction="S", kind="issue", session="x")
        raise AssertionError("expected RuntimeError")
    except RuntimeError:
        pass


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ✓ {t.__name__}")
    print(f"All {len(tests)} llm tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
