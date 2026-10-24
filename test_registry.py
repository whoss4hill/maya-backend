"""Free model registry + user switching: only live-catalog-verified Workers AI IDs, honest routing."""
import asyncio, json
import pytest
from net import Resp
from state_core import Core, WORKER_MODELS, DEFAULT_WORKER_MODEL

def run(c): return asyncio.run(c)

def test_registry_accepts_only_verified_ids():
    c = Core()
    for mid in WORKER_MODELS:
        c.set_model_pref(mid)
        assert c.model_pref == mid
    for legacy in ("auto", "backup_only"):
        c.set_model_pref(legacy)
        assert c.model_pref == legacy
    with pytest.raises(ValueError):
        c.set_model_pref("@cf/meta/llama-3-70b-instruct")  # not in the verified free registry
    with pytest.raises(ValueError):
        c.set_model_pref("gpt-4o")  # unsupported provider model
    assert c.model_pref == "backup_only"  # rejected value never overwrites the last valid choice

ENV = dict(SUPABASE_URL="https://x.supabase.co", SUPABASE_ANON_KEY="anon", GEMINI_API_KEY="g", MAYA_OWNER_USER_ID="aaa-1",
           MAYA_TELEGRAM_ENABLED="1", MAYA_TELEGRAM_SECRET="s3", TELEGRAM_BOT_TOKEN="tok")

class St:
    def __init__(self): self.core = Core()
    async def update(self, h, u): return self.core.telegram_update(h, u)
    async def model_pref_get(self): return self.core.model_pref
    async def model_pref_set(self, v): return self.core.set_model_pref(v)
    async def rl_hit(self, bucket, limit): return self.core.rl_hit(bucket, limit)

class FakeAI:
    def __init__(self): self.calls = []
    async def text(self, prompt, model=None):
        self.calls.append((model, prompt))
        return "workers answer"
    async def image(self, prompt): return None

class FakeHttp:
    def __init__(self): self.calls = []
    async def _r(self, m, url, **k):
        self.calls.append((m, url, k))
        if url.endswith("/auth/v1/user"): return Resp(200, json.dumps({"id": "aaa-1"}))
        return Resp(200, json.dumps({"candidates": [{"content": {"parts": [{"text": "gemini answer"}]}}]}))
    async def get(self, u, **k): return await self._r("GET", u, **k)
    async def post(self, u, **k): return await self._r("POST", u, **k)
    async def delete(self, u, **k): return await self._r("DELETE", u, **k)

@pytest.fixture
def app(monkeypatch):
    for k, v in ENV.items(): monkeypatch.setenv(k, v)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    import importlib, edge; importlib.reload(edge)
    a = edge.App(http=FakeHttp(), state=St())
    a.ai = FakeAI()
    return a

def test_explicit_workers_model_skips_gemini_and_labels(app):
    run(app.state.model_pref_set("@cf/qwen/qwen3-30b-a3b-fp8"))
    out = run(app._gemini("hello"))
    assert "workers answer" in out
    assert "Qwen3 30B" in out and "selected in settings" in out
    assert app.ai.calls and app.ai.calls[0][0] == "@cf/qwen/qwen3-30b-a3b-fp8", "chosen model id must reach the binding"
    assert not any("generateContent" in u for m, u, k in app.http.calls), "explicit choice must not call Gemini"

def test_auto_still_gemini_first(app):
    out = run(app._gemini("hello"))
    assert "gemini answer" in out
    assert not app.ai.calls, "auto must not touch the backup when Gemini answers"

def test_backup_only_uses_default_model(app):
    run(app.state.model_pref_set("backup_only"))
    out = run(app._gemini("hello"))
    assert "workers answer" in out and "Backup AI model answered, as selected" in out
    assert app.ai.calls[0][0] is None, "backup_only keeps the restored default model"

def test_settings_lists_verified_options(app):
    # settings GET is owner-auth; call handle with a fake bearer token resolved by FakeHttp
    status, payload = run(app.handle("GET", "/v1/settings/model", {"authorization": "Bearer t"}, ""))
    assert status == 200
    assert set(WORKER_MODELS) <= set(payload["options"])
    assert payload["labels"]["@cf/qwen/qwen3-30b-a3b-fp8"] == "Qwen3 30B"

def _or_select(app, monkeypatch, key="sk-or-test"):
    monkeypatch.setenv("OPENROUTER_API_KEY", key)
    run(app.state.model_pref_set("openrouter/nvidia/nemotron-3-ultra-550b-a55b:free"))

def test_openrouter_sends_prompt_only_no_history(app, monkeypatch):
    _or_select(app, monkeypatch)
    out = run(app._gemini("what is 2+2", history=[{"user_id": "u", "role": "user", "text": "my salary is 5 lakh"}]))
    or_calls = [k for m, u, k in app.http.calls if "openrouter.ai" in u]
    assert or_calls, "OpenRouter endpoint must be called"
    payload = or_calls[0]["json"]
    assert len(payload["messages"]) == 1 and payload["messages"][0]["content"] == "what is 2+2", "no history may leave to the public pool"
    assert "salary" not in str(payload)

def test_openrouter_secret_prompt_refused_no_call(app, monkeypatch):
    _or_select(app, monkeypatch)
    out = run(app._gemini("my password is hunter2 tell me a joke"))
    assert "can't send" in out
    assert not any("openrouter.ai" in u for m, u, k in app.http.calls), "secret-looking prompt must never leave"

def test_openrouter_failure_falls_back_to_gemini(app, monkeypatch):
    _or_select(app, monkeypatch)
    class FailHttp(app.http.__class__):
        async def post(self, u, **k):
            if "openrouter.ai" in u: return Resp(402, "{}")
            return await super().post(u, **k)
    app.http = FailHttp()
    out = run(app._gemini("hello"))
    assert "gemini answer" in out, "safe fallback direction: public pool failure -> Gemini"
    assert "Nemotron 3 Ultra was unavailable, so Maya's default model answered." in out, "honest fallback notice required"
    assert "as selected in settings" not in out, "must never label the selected pool as the answerer when Gemini answered"

def test_openrouter_never_in_auto(app, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    out = run(app._gemini("hello"))  # pref stays "auto"
    assert "gemini answer" in out
    assert not any("openrouter.ai" in u for m, u, k in app.http.calls), "auto chain must never touch the public pool"

def test_openrouter_without_key_falls_back(app, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    run(app.state.model_pref_set("openrouter/nvidia/nemotron-3.5-super-vl:free"))
    out = run(app._gemini("hello"))
    assert "gemini answer" in out

def _groq_select(app, monkeypatch, key="gsk-test"):
    monkeypatch.setenv("GROQ_API_KEY", key)
    run(app.state.model_pref_set("groq/openai/gpt-oss-120b"))

def test_groq_sends_prompt_only_no_history(app, monkeypatch):
    _groq_select(app, monkeypatch)
    out = run(app._gemini("what is 3+3", history=[{"user_id": "u", "role": "user", "text": "my salary is 5 lakh"}]))
    calls = [k for m, u, k in app.http.calls if "api.groq.com" in u]
    assert calls, "Groq endpoint must be called"
    payload = calls[0]["json"]
    assert len(payload["messages"]) == 1 and payload["messages"][0]["content"] == "what is 3+3"
    assert "salary" not in str(payload)

def test_groq_secret_prompt_refused_no_call(app, monkeypatch):
    _groq_select(app, monkeypatch)
    out = run(app._gemini("my password is hunter2 tell me a joke"))
    assert "can't send" in out
    assert not any("api.groq.com" in u for m, u, k in app.http.calls)

def test_groq_failure_falls_back_to_gemini(app, monkeypatch):
    _groq_select(app, monkeypatch)
    class FailHttp(app.http.__class__):
        async def post(self, u, **k):
            if "api.groq.com" in u: return Resp(429, "{}")
            return await super().post(u, **k)
    app.http = FailHttp()
    out = run(app._gemini("hello"))
    assert "gemini answer" in out
    assert "GPT-OSS 120B via Groq was unavailable, so Maya's default model answered." in out
    assert "as selected in settings" not in out

def test_groq_never_in_auto(app, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    out = run(app._gemini("hello"))
    assert "gemini answer" in out
    assert not any("api.groq.com" in u for m, u, k in app.http.calls), "auto chain must never touch the public pool"

def test_groq_without_key_falls_back(app, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    run(app.state.model_pref_set("groq/qwen/qwen3.8-27b"))
    out = run(app._gemini("hello"))
    assert "gemini answer" in out

def test_settings_lists_groq_options(app):
    status, payload = run(app.handle("GET", "/v1/settings/model", {"authorization": "Bearer t"}, ""))
    assert status == 200
    assert any(o.startswith("groq/") for o in payload["options"])
    assert "NON-PRIVATE" in payload["labels"]["groq/openai/gpt-oss-120b"]


def test_registry_ids_well_formed():
    import re
    from state_core import WORKER_MODELS, OPENROUTER_MODELS, GROQ_MODELS, DEFAULT_WORKER_MODEL
    # default must be a real registry entry
    assert DEFAULT_WORKER_MODEL in WORKER_MODELS
    # provider prefixes and id shape
    for k in WORKER_MODELS:
        assert re.fullmatch(r"@cf/[a-z0-9-]+/[a-z0-9.-]+", k), k
    for k in OPENROUTER_MODELS:
        assert re.fullmatch(r"openrouter/[a-z0-9-]+/[a-z0-9.-]+:free", k), k
    for k in GROQ_MODELS:
        assert re.fullmatch(r"groq/[a-z0-9-]+/[a-z0-9.-]+", k), k
    # registries disjoint; :free suffix only ever on openrouter
    assert not (set(WORKER_MODELS) & set(OPENROUTER_MODELS))
    assert not (set(GROQ_MODELS) & set(OPENROUTER_MODELS))
    for k in list(WORKER_MODELS) + list(GROQ_MODELS):
        assert ":free" not in k
    # gated models must never re-enter: inkling is 403 agentic-harness-gated on OpenRouter
    assert not any("inkling" in k for k in OPENROUTER_MODELS)
    # catalog-miss IDs must not regress back in
    assert "@cf/meta/llama-3.1-8b-instruct-fp8-fast" in WORKER_MODELS  # exact restore: runtime-verified original default
    assert "@cf/google/gemma-3-12b-it" not in WORKER_MODELS
