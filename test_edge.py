import os, json, asyncio, time, pytest
from net import Resp
from state_core import Core

ENV = dict(SUPABASE_URL="https://x.supabase.co", SUPABASE_ANON_KEY="anon", GEMINI_API_KEY="g", MAYA_OWNER_USER_ID="aaa-1",
           MAYA_TELEGRAM_ENABLED="1", MAYA_TELEGRAM_SECRET="s3", TELEGRAM_BOT_TOKEN="tok")

class St:
    def __init__(self): self.core = Core()
    async def update(self, h, u): return self.core.telegram_update(h, u)
    async def pair_start(self): return self.core.pair_start()
    async def set_enabled(self, on): self.core.set_enabled(on)
    async def revoke(self): self.core.revoke()
    async def status(self): return {"bound": self.core.bound_id is not None, "enabled": self.core.data["tg"]["owner_enabled"], "bound_suffix": None}

class FakeHttp:
    def __init__(self): self.calls = []
    async def _r(self, m, url, **k):
        self.calls.append((m, url, k))
        if url.endswith("/auth/v1/user"): return Resp(200, json.dumps({"id": "aaa-1"}))
        if "generateContent" in url: return Resp(200, json.dumps({"candidates": [{"content": {"parts": [{"text": "pong"}]}}]}))
        if "/rest/v1/memories" in url and m == "POST": return Resp(201, json.dumps([{"id": "m1"}]))
        return Resp(200, "[]")
    async def get(self, u, **k): return await self._r("GET", u, **k)
    async def post(self, u, **k): return await self._r("POST", u, **k)
    async def delete(self, u, **k): return await self._r("DELETE", u, **k)

@pytest.fixture
def app(monkeypatch):
    for k, v in ENV.items(): monkeypatch.setenv(k, v)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    import importlib, edge; importlib.reload(edge)
    return edge.App(http=FakeHttp(), state=St())

def run(c): return asyncio.run(c)
A = {"Authorization": "Bearer jwt"}

def test_health_404_and_auth(app):
    assert run(app.handle("GET", "/health", {}, ""))[0] == 200
    assert run(app.handle("GET", "/x", {}, ""))[0] == 404
    assert run(app.handle("GET", "/v1/memories", {}, ""))[0] == 401

def test_jwt_headers_and_no_service_role(app):
    run(app.handle("GET", "/v1/tasks", A, ""))
    for _, url, k in app.http.calls:
        assert k["headers"] == {"apikey": "anon", "Authorization": "Bearer jwt"}

def test_chat_memory_task_flow(app):
    s, b = run(app.handle("POST", "/v1/chat", A, json.dumps({"message": "hi"}))); assert (s, b) == (200, {"reply": "pong"})
    assert run(app.handle("POST", "/v1/memories", A, json.dumps({"kind": "fact", "content": "x"}))) == (201, {"id": "m1"})
    assert run(app.handle("DELETE", "/v1/memories/abc-1", A, ""))[0] == 204
    assert run(app.handle("POST", "/v1/tasks", A, "{}"))[0] == 400

def test_other_user_forbidden(app, monkeypatch):
    app.owner = "someone-else"; assert run(app.handle("GET", "/v1/tasks", A, ""))[0] == 403

def test_refuses_service_role(monkeypatch):
    for k, v in ENV.items(): monkeypatch.setenv(k, v)
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "svc")
    import edge
    with pytest.raises(RuntimeError): edge.App(http=FakeHttp(), state=St())

def test_telegram_pair_command_toggle(app):
    H = {"X-Telegram-Bot-Api-Secret-Token": "s3"}
    code = run(app.handle("POST", "/v1/telegram/pair/start", A, ""))[1]["code"]
    def tg(i, uid, text): return run(app.handle("POST", "/telegram/webhook", H, json.dumps({"update_id": i, "message": {"from": {"id": uid}, "chat": {"id": uid, "type": "private"}, "text": text, "date": int(time.time())}})))
    assert tg(1, 42, f"/pair {code}") == (200, {}) and not any("telegram.org" in u for _, u, _ in app.http.calls)
    tg(2, 42, "ping"); sent = [k["json"] for _, u, k in app.http.calls if "sendMessage" in u]; assert sent == [{"chat_id": 42, "text": "pong"}]
    tg(3, 99, "ping"); assert len([1 for _, u, _ in app.http.calls if "sendMessage" in u]) == 1
    run(app.handle("POST", "/v1/telegram/toggle", A, json.dumps({"on": False}))); tg(4, 42, "ping")
    assert len([1 for _, u, _ in app.http.calls if "sendMessage" in u]) == 1
