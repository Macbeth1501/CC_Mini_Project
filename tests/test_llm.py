"""Tests for the explanation layer: templates, grounding check, providers, HTTP shape, cache, fallback."""
import io
import json
import os
import urllib.request

import pytest

import cfgtool

FINDING = {"kind": "uninit", "line": 5, "var": "result", "block": 5, "depth": 0, "cost": 2, "rank": 9,
           "source": "total = result + 1", "path": ["ENTRY", "B2", "B4", "B5"], "value": None}


def finding(**kw):
    """A copy of FINDING with some fields replaced."""
    return {**FINDING, **kw}


# ---------------------------------------------------------------- templates
@pytest.mark.parametrize("kind,needle", [
    ("uninit", "may be used at line 5 before it is assigned"),
    ("unreachable", "can never execute"),
    ("dead_store", "never used afterwards"),
    ("infinite_loop", "can never exit"),
    ("constant_condition", "always"),
])
def test_template_exists_for_every_kind(kind, needle):
    assert needle in cfgtool.template(finding(kind=kind, value=True))


def test_uninit_template_mentions_the_evidence_path():
    assert "ENTRY -> B2 -> B4 -> B5" in cfgtool.template(FINDING)


def test_constant_condition_template_says_true_or_false():
    assert "always true" in cfgtool.template(finding(kind="constant_condition", value=True))
    assert "always false" in cfgtool.template(finding(kind="constant_condition", value=False))


def test_every_severity_kind_has_a_template():
    for kind in cfgtool.SEVERITY:
        assert cfgtool.template(finding(kind=kind, value=False))


# ---------------------------------------------------------------- grounding
@pytest.mark.parametrize("reply,ok", [
    ("`result` is used at line 5; assign it first.", True),
    ("`result` is used at line 99.", False),                       # wrong line
    ("`ghost` is used at line 5.", False),                         # invented identifier
    ("Initialise it, e.g. `result = 0`, before line 5.", True),    # fix snippets may add literals
    ("Initialise `result = 0` before line 12.", False),            # wrong line in prose
    ("Set `result = None` first.", True),                          # keywords allowed in snippets
    ('Use `result = "x"` first.', True),                           # string literals ignored
    ("Set `ghost = 0` first.", False),                             # invented variable in a snippet
    ("The path ENTRY -> B2 -> B4 -> B5 never assigns `result`, line 5.", True),   # block ids from the evidence
    ("Block 77 reads `result` at line 5.", False),                 # block id not in the finding
    ("Call `print` on `result` at line 5.", True),                 # builtins are fine
    ("`result` is unset in block `B5` along `ENTRY → B2 → B4 → B5`.", True),   # block names from the path
    ("`result` is unset along `ENTRY -> B2 -> B4 -> B5`.", True),
    ("`result` is unset in block `B99`.", False),                  # invented block name
    ("`result` is unset along `ENTRY -> B2 -> B77 -> B5`.", False),  # invented block inside a path
])
def test_grounding_check(reply, ok):
    assert cfgtool.grounded(reply, FINDING) is ok


def test_constant_condition_replies_may_say_true_or_false():
    f = finding(kind="constant_condition", var=None, source="if 1 > 2:", value=False, path=[], block=0)
    assert cfgtool.grounded("The `if 1 > 2:` is always `false`, line 5.", f)
    assert not cfgtool.grounded("The `if 1 > 2:` is always `false`, line 5.", finding(value=None, var="result"))


def test_explain_falls_back_when_the_llm_hallucinates_or_fails():
    assert cfgtool.explain(FINDING, True, client=lambda p: "`ghost` at line 42")[1] == "template"
    assert cfgtool.explain(FINDING, True, client=lambda p: None)[1] == "template"
    assert cfgtool.explain(FINDING, False, client=lambda p: pytest.fail("must not be called"))[1] == "template"


def test_explain_accepts_a_grounded_reply_and_normalises_whitespace():
    text, origin = cfgtool.explain(FINDING, True, client=lambda p: "`result` at   line\n5 is unset.")
    assert origin == "llm" and text == "`result` at line 5 is unset."


def test_prompt_contains_only_finding_facts():
    seen = []
    cfgtool.explain(FINDING, True, client=lambda p: seen.append(p))
    prompt = seen[0]
    assert '"line": 5' in prompt and '"var": "result"' in prompt and "ENTRY" in prompt
    assert "ONLY facts" in prompt and "backticks" in prompt


# ---------------------------------------------------------------- providers / HTTP / cache
@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    for k in ("GROQ_API_KEY", "ANTHROPIC_API_KEY", "LLM_PROVIDER", "GROQ_MODEL", "ANTHROPIC_MODEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(cfgtool, "CACHE_FILE", str(tmp_path / "cache.json"))
    return monkeypatch


def test_provider_selection(clean_env):
    assert cfgtool.llm_provider() is None
    clean_env.setenv("ANTHROPIC_API_KEY", "a")
    assert cfgtool.llm_provider() == "anthropic"
    clean_env.setenv("GROQ_API_KEY", "g")
    assert cfgtool.llm_provider() == "groq"                        # Groq wins when both are set
    clean_env.setenv("LLM_PROVIDER", "anthropic")
    assert cfgtool.llm_provider() == "anthropic"                   # unless forced
    clean_env.setenv("LLM_PROVIDER", "nonsense")
    assert cfgtool.llm_provider() == "groq"


def test_model_override(clean_env):
    assert cfgtool.llm_model("groq") == cfgtool.PROVIDERS["groq"]["model"]
    clean_env.setenv("GROQ_MODEL", "my-model")
    assert cfgtool.llm_model("groq") == "my-model"


class FakeResponse(io.BytesIO):
    """Minimal context-manager response for a patched urlopen."""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def fake_urlopen(capture, payload):
    """Return a urlopen replacement that records the request and answers with `payload`."""
    def urlopen(req, timeout=0):
        capture["url"] = req.full_url
        capture["headers"] = {k.lower(): v for k, v in req.header_items()}
        capture["body"] = json.loads(req.data)
        return FakeResponse(json.dumps(payload).encode())
    return urlopen


def test_groq_request_shape_and_reply_parsing(clean_env):
    clean_env.setenv("GROQ_API_KEY", "gk")
    cap = {}
    clean_env.setattr(urllib.request, "urlopen", fake_urlopen(cap, {"choices": [{"message": {"content": " hi there "}}]}))
    assert cfgtool.llm_call("prompt") == "hi there"
    assert cap["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert cap["headers"]["authorization"] == "Bearer gk" and "user-agent" in cap["headers"]
    assert cap["body"]["messages"] == [{"role": "user", "content": "prompt"}] and cap["body"]["temperature"] == 0


def test_anthropic_request_shape_and_reply_parsing(clean_env):
    clean_env.setenv("ANTHROPIC_API_KEY", "ak")
    cap = {}
    clean_env.setattr(urllib.request, "urlopen", fake_urlopen(cap, {"content": [{"text": "hello"}]}))
    assert cfgtool.llm_call("prompt") == "hello"
    assert cap["url"] == "https://api.anthropic.com/v1/messages"
    assert cap["headers"]["x-api-key"] == "ak" and cap["headers"]["anthropic-version"] == "2023-06-01"


def test_replies_are_cached_so_a_second_call_needs_no_network(clean_env):
    clean_env.setenv("GROQ_API_KEY", "gk")
    calls = []

    def urlopen(req, timeout=0):
        calls.append(1)
        return FakeResponse(json.dumps({"choices": [{"message": {"content": "once"}}]}).encode())

    clean_env.setattr(urllib.request, "urlopen", urlopen)
    assert cfgtool.llm_call("same") == "once" and cfgtool.llm_call("same") == "once"
    assert len(calls) == 1
    clean_env.setenv("GROQ_API_KEY", "now-invalid")
    assert cfgtool.llm_call("same") == "once"                      # still served from the cache


def test_failures_return_none_and_record_the_reason(clean_env):
    assert cfgtool.llm_call("x") is None and "no API key" in cfgtool.LAST_LLM_ERROR
    clean_env.setenv("GROQ_API_KEY", "gk")

    def boom(req, timeout=0):
        raise OSError("HTTP Error 401: Unauthorized")

    clean_env.setattr(urllib.request, "urlopen", boom)
    assert cfgtool.llm_call("y") is None and "401" in cfgtool.LAST_LLM_ERROR
    clean_env.setattr(urllib.request, "urlopen", fake_urlopen({}, {"unexpected": 1}))
    assert cfgtool.llm_call("z") is None and "request failed" in cfgtool.LAST_LLM_ERROR   # malformed reply
    assert not os.path.exists(cfgtool.CACHE_FILE)                                          # failures are not cached
