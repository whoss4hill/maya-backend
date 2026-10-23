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
    async def bound(self): return self.core.bound_id
    async def status(self): return {"bound": self.core.bound_id is not None, "enabled": self.core.data["tg"]["owner_enabled"], "bound_suffix": None}

class FakeHttp:
    def __init__(self): self.calls = []
    async def _r(self, m, url, **k):
        self.calls.append((m, url, k))
        if url.endswith("/auth/v1/user"): return Resp(200, json.dumps({"id": "aaa-1"}))
        if "generateContent" in url: return Resp(200, json.dumps({"candidates": [{"content": {"parts": [{"text": "pong"}]}}]}))
        if "/rest/v1/memories" in url and m == "POST": return Resp(201, json.dumps([{"id": "m1"}]))
        return Resp(200, "[]")
    async def post_file(self, url, fields, filename, data, mime="application/pdf", field="document"):
        self.calls.append(("FILE", url, {"fields": fields, "filename": filename, "data": data}))
        return Resp(200, "{}")
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
    assert tg(1, 42, f"/pair {code}") == (200, {})
    sent = [k["json"] for _, u, k in app.http.calls if "sendMessage" in u]; assert len(sent) == 1 and sent[0]["chat_id"] == 42 and "Paired" in sent[0]["text"]
    assert "pong" not in sent[0]["text"] and not any("generateContent" in u for _, u, _ in app.http.calls)
    tg(2, 42, "ping"); sent = [k["json"] for _, u, k in app.http.calls if "sendMessage" in u]; assert sent[-1] == {"chat_id": 42, "text": "pong"}
    n = len(sent)
    tg(3, 99, "ping"); assert len([1 for _, u, _ in app.http.calls if "sendMessage" in u]) == n
    tg(5, 42, "/pdf cat facts"); f = [k for m, _, k in app.http.calls if m == "FILE"]
    assert len(f) == 1 and f[0]["fields"]["chat_id"] == 42 and f[0]["fields"]["caption"].startswith("PDF: ") and f[0]["data"][:5] == b"%PDF-"
    tg(6, 42, "/image a dog"); assert "not enabled" in [k["json"] for _, u, k in app.http.calls if "sendMessage" in u][-1]["text"]
    tg(7, 99, "/pdf x"); assert len([1 for m, _, _ in app.http.calls if m == "FILE"]) == 1
    run(app.handle("POST", "/v1/telegram/toggle", A, json.dumps({"on": False}))); k0 = len(app.http.calls); tg(8, 42, "ping")
    assert not any("sendMessage" in u for _, u, _ in app.http.calls[k0:])

def test_pdf_hindi_refused_clear_message(app):
    H = {"X-Telegram-Bot-Api-Secret-Token": "s3"}
    code = run(app.handle("POST", "/v1/telegram/pair/start", A, ""))[1]["code"]
    def tg(i, text): return run(app.handle("POST", "/telegram/webhook", H, json.dumps({"update_id": i, "message": {"from": {"id": 42}, "chat": {"id": 42, "type": "private"}, "text": text, "date": int(time.time())}})))
    tg(1, f"/pair {code}"); tg(2, "/pdf \u0928\u092e\u0938\u094d\u0924\u0947")
    assert not [1 for m, _, _ in app.http.calls if m == "FILE"]
    assert "Latin" in [k["json"] for _, u, k in app.http.calls if "sendMessage" in u][-1]["text"]

def test_long_reply_is_chunked_not_truncated(app):
    long = ("line of code\n" * 700)
    ch = app.chunks(long)
    assert len(ch) >= 3 and all(len(c) <= 4000 for c in ch)
    assert "".join(c if i == 0 else "\n" + c for i, c in enumerate(ch)).count("line of code") == 700
    assert len(app.chunks("x" * 100000)) == 10 and "truncated" in app.chunks("x" * 100000)[-1]

def test_gemini_retry_and_typing(app, monkeypatch):
    import edge, asyncio as aio
    async def nosleep(s): pass
    monkeypatch.setattr(edge.asyncio, "sleep", nosleep)
    n = {"c": 0}
    orig = app.http._r
    async def flaky(m, url, **k):
        if "generateContent" in url:
            n["c"] += 1
            if n["c"] < 3:
                app.http.calls.append((m, url, k)); return Resp(429, "{}")
        return await orig(m, url, **k)
    app.http._r = flaky
    assert run(app.chat_probe()) == "pong" if hasattr(app, "chat_probe") else True
    r = run(app._gemini("x")); assert r == "pong" and n["c"] == 3
    urls = [u for _, u, _ in app.http.calls if "generateContent" in u]
    assert len({u.split("/models/")[1].split(":")[0] for u in urls}) >= 2  # fell back to another model
    run(app._tg_process("ok", "hello", 42))
    acts = [k["json"] for _, u, k in app.http.calls if "sendChatAction" in u]
    assert acts and acts[0] == {"chat_id": 42, "action": "typing"}
    run(app._tg_process("ok", "/pdf notes", 42))
    assert any(k["json"].get("action") == "upload_document" for _, u, k in app.http.calls if "sendChatAction" in u)


def test_gemini_payload_has_truthfulness_rule(app):
    p = app._gem("hi"); s = p["system_instruction"]["parts"][0]["text"]
    assert "NO internet" in s and "Never claim to have checked" in s and p["contents"][0]["parts"][0]["text"] == "hi"


def test_voice_tts_wraps_pcm_and_sends_audio(app, monkeypatch):
    import base64
    pcm = b"\x00\x01" * 100
    async def post(u, **k):
        app.http.calls.append(("POST", u, k))
        if "3.8-flash-tts" in u: return Resp(429, "{}")
        return Resp(200, json.dumps({"candidates": [{"content": {"parts": [{"inlineData": {"mimeType": "audio/L16; rate=24000; channels=1", "data": base64.b64encode(pcm).decode()}}]}}]}))
    app.http.post = post
    w = run(app._tts("hello")); assert w[:4] == b"RIFF" and pcm in w
    run(app._tg_process("ok", "/voice hello", 42))
    f = [k for m, _, k in app.http.calls if m == "FILE" and "sendAudio" in _]
    assert f and f[0]["filename"] == "maya-voice.wav" and f[0]["data"][:4] == b"RIFF"


def test_chat_history_passed_and_no_intro_rule(app):
    app.state.hist = []
    async def history(): return list(app.state.hist)
    async def add_turn(u, m): app.state.hist += [["user", u], ["model", m]]
    app.state.history, app.state.add_turn = history, add_turn
    run(app._tg_process("ok", "hi", 42)); run(app._tg_process("ok", "aur batao", 42))
    gens = [k["json"] for m, u, k in app.http.calls if "generateContent" in u]
    assert len(gens[0]["contents"]) == 1 and len(gens[1]["contents"]) == 3
    assert gens[1]["contents"][0] == {"role": "user", "parts": [{"text": "hi"}]} and gens[1]["contents"][1]["role"] == "model"
    assert "never introduce yourself" in gens[1]["system_instruction"]["parts"][0]["text"]


def test_call_command_offline_and_sent(app):
    sent = []
    async def push(cmd): sent.append(cmd); return "offline"
    app.state.push_command = push
    run(app._tg_process("ok", "call Mom", 42))
    assert sent and sent[0]["cmd"] == "call" and sent[0]["target"] == "Mom"
    assert not any("generateContent" in u for _, u, _ in app.http.calls)
    msgs = [k["json"]["text"] for _, u, k in app.http.calls if "sendMessage" in u]
    assert "Nothing was dialled" in msgs[-1]
