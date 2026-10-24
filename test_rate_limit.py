"""Maya per-minute burst limit (Maya's own, not a provider quota): bucket classification, 429 shape,
env override, rollover, counter failure. Fake state only - no network beyond the mocked auth."""
import os, json, asyncio, time, pytest
from test_edge import ENV, St, FakeHttp, run

A = {"Authorization": "Bearer jwt"}

@pytest.fixture
def app(monkeypatch):
    for k, v in ENV.items(): monkeypatch.setenv(k, v)
    monkeypatch.setenv("MAYA_RL_READ_PER_MIN", "2")
    monkeypatch.setenv("MAYA_RL_CHAT_PER_MIN", "1")
    import importlib, edge; importlib.reload(edge)
    return edge.App(http=FakeHttp(), state=St())

def test_bucket_classification(app):
    assert app._rl_bucket("POST", "/v1/chat") == "chat"
    assert app._rl_bucket("POST", "/v1/image") == "image"
    assert app._rl_bucket("GET", "/v1/settings/model") == "read"
    assert app._rl_bucket("GET", "/v1/memories") == "read"
    assert app._rl_bucket("POST", "/v1/settings/model") == "write"
    assert app._rl_bucket("POST", "/v1/memories") == "write"

def test_read_bucket_429_shape(app):
    s1, b1 = run(app.handle("GET", "/v1/settings/model", A, None))
    s2, b2 = run(app.handle("GET", "/v1/settings/model", A, None))
    s3, b3 = run(app.handle("GET", "/v1/settings/model", A, None))
    assert (s1, s2) == (200, 200)
    assert s3 == 429
    assert b3["error"] == "maya_rate_limited"
    assert b3["bucket"] == "read"
    assert 1 <= b3["retry_after_s"] <= 60
    assert "not a provider quota" in b3["note"]

def test_buckets_independent(app):
    # read bucket at its 2/min cap; a write to settings/model uses the separate write bucket
    run(app.handle("GET", "/v1/settings/model", A, None))
    run(app.handle("GET", "/v1/settings/model", A, None))
    s, b = run(app.handle("POST", "/v1/settings/model", A, json.dumps({"model": "auto"})))
    assert s == 200

def test_chat_bucket_env_override(app):
    # MAYA_RL_CHAT_PER_MIN=1: second chat POST in the same minute is limited before any model call
    s1, _ = run(app.handle("POST", "/v1/chat", A, json.dumps({"message": "hi"})))
    s2, b2 = run(app.handle("POST", "/v1/chat", A, json.dumps({"message": "again"})))
    assert s1 == 200
    assert s2 == 429 and b2["bucket"] == "chat"

def test_minute_rollover_resets():
    from state_core import Core
    c = Core()
    base = 1792838400.0  # exact minute boundary
    assert c.rl_hit("read", 2, now=base) == (True, 0)
    assert c.rl_hit("read", 2, now=base + 1) == (True, 0)
    allowed, retry = c.rl_hit("read", 2, now=base + 2)
    assert allowed is False and retry == 58
    # next minute: fresh counter
    assert c.rl_hit("read", 2, now=base + 60) == (True, 0)

def test_invalid_env_falls_back(app, monkeypatch):
    monkeypatch.setenv("MAYA_RL_READ_PER_MIN", "garbage")
    assert app._rl_bucket("GET", "/v1/x") == "read"
    # garbage env -> default 60: a few reads all pass
    for _ in range(3):
        s, _ = run(app.handle("GET", "/v1/settings/model", A, None))
        assert s == 200

def test_counter_error_503(monkeypatch):
    for k, v in ENV.items(): monkeypatch.setenv(k, v)
    import importlib, edge; importlib.reload(edge)
    class Boom(St):
        async def rl_hit(self, bucket, limit): raise RuntimeError("do down")
    app = edge.App(http=FakeHttp(), state=Boom())
    s, b = run(app.handle("GET", "/v1/settings/model", A, None))
    assert s == 503 and b == {"error": "rate_counter_unavailable"}

def test_auth_runs_before_limit(app):
    # unauthenticated requests must not consume or reveal buckets
    s, b = run(app.handle("GET", "/v1/settings/model", {}, None))
    assert s == 401
    s, b = run(app.handle("GET", "/v1/settings/model", A, None))
    assert s == 200
