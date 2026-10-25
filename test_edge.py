import asyncio
import os, json, asyncio, time, pytest
from net import Resp
from state_core import Core

ENV = dict(SUPABASE_URL="https://x.supabase.co", SUPABASE_ANON_KEY="anon", GEMINI_API_KEY="g", MAYA_OWNER_USER_ID="aaa-1",
           MAYA_TELEGRAM_ENABLED="1", MAYA_TELEGRAM_SECRET="s3", TELEGRAM_BOT_TOKEN="tok")

class St:
    def __init__(self): self.core = Core()
    async def update(self, h, u): return self.core.telegram_update(h, u)
    async def pair_start(self, target_id=None): return self.core.pair_start(target_id=target_id)
    async def set_enabled(self, on): self.core.set_enabled(on)
    async def voice_mode_get(self): return self.core.voice_mode
    async def voice_mode_set(self, on): self.core.set_voice_mode(on)
    async def image_mode_get(self): return self.core.image_mode
    async def image_mode_set(self, on): self.core.set_image_mode(on)
    async def fail_add(self, stage, status): self.core.fail_add(stage, status)
    async def revoke(self): self.core.revoke()
    async def model_pref_get(self): return self.core.model_pref
    async def model_pref_set(self, v): self.core.set_model_pref(v)
    async def rl_hit(self, bucket, limit): return self.core.rl_hit(bucket, limit)
    async def bound(self): return self.core.bound_id
    job = {}
    async def job_set(self, s, n="", alarm_s=0): self.job = {"state": s, "note": n, "ts": time.time()}
    async def job_get(self): return self.job
    async def status(self): return {"bound": self.core.bound_id is not None, "enabled": self.core.data["tg"]["owner_enabled"], "bound_suffix": None}

class FakeHttp:
    stt_status = 200
    def __init__(self): self.calls = []
    async def get_bytes(self, u, **k):
        self.calls.append(("GETB", u, k))
        return 200, b"FILEBYTES"
    async def _r(self, m, url, **k):
        self.calls.append((m, url, k))
        if url.endswith("/auth/v1/user"): return Resp(200, json.dumps({"id": "aaa-1"}))
        if "getFile" in url: return Resp(200, json.dumps({"ok": True, "result": {"file_path": "files/p1.oga"}}))
        if "en.wikipedia.org/w/api.php" in url: return Resp(200, json.dumps(["q", ["ISRO"], [""], ["https://en.wikipedia.org/wiki/ISRO"]]))
        if "api/rest_v1/page/summary/" in url: return Resp(200, json.dumps({"title": "ISRO", "extract": "The Indian Space Research Organisation is India's national space agency.", "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/ISRO"}}}))
        if "gdeltproject" in url:
            if getattr(self, "gdelt_status", 200) != 200: return Resp(self.gdelt_status, "{}")
            return Resp(200, json.dumps({"articles": [{"title": "ISRO announces new mission", "url": "https://www.rediff.com/news/isro.htm", "domain": "rediff.com", "seendate": "20261025120000"}]}))
        if "generateContent" in url:
            if getattr(self, "raise_on_gen", False): raise TimeoutError("simulated provider timeout")
            if self.stt_status != 200: return Resp(self.stt_status, "{}")
            return Resp(200, json.dumps({"candidates": [{"content": {"parts": [{"text": "pong"}]}}]}))
        if "auth_tokens" in url: return Resp(200, json.dumps({"name": "auth_tokens/deadbeef1234", "expireTime": "2099-01-01T00:00:00Z"}))
        if "/rest/v1/memories" in url and m == "POST": return Resp(201, json.dumps([{"id": "m1"}]))
        return Resp(200, "[]")
    async def post_file(self, url, fields, filename, data, mime="application/pdf", field="document"):
        self.calls.append(("FILE", url, {"fields": fields, "filename": filename, "data": data, "mime": mime, "field": field}))
        return Resp(getattr(self, "file_status", 200), json.dumps({"ok": getattr(self, "file_ok", True), "result": {"message_id": 1}}))
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
    tg(6, 42, "/image a dog"); assert "not set up on this server" in [k["json"] for _, u, k in app.http.calls if "sendMessage" in u][-1]["text"]
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
    w, ext, mime = run(app._tts("hello")); assert w[:4] == b"RIFF" and pcm in w and (ext, mime) == ("wav", "audio/wav")
    run(app._tg_process("ok", "/voice hello", 42))
    f = [k for m, _, k in app.http.calls if m == "FILE" and "sendAudio" in _]
    assert f and f[0]["filename"] == "maya-voice.wav" and f[0]["data"][:4] == b"RIFF"


def test_elevenlabs_off_by_default(app):
    # Without explicit env config the Gemini chain is the only TTS path
    import base64
    pcm = b"\x00\x01" * 100
    async def post(u, **k):
        return Resp(200, json.dumps({"candidates": [{"content": {"parts": [{"inlineData": {"mimeType": "audio/L16; rate=24000; channels=1", "data": base64.b64encode(pcm).decode()}}]}}]}))
    app.http.post = post
    async def post_bytes(u, headers=None, json=None, timeout=None):
        raise AssertionError("elevenlabs must not be called when unconfigured")
    app.http.post_bytes = post_bytes
    w, ext, _ = run(app._tts("hello")); assert ext == "wav" and w[:4] == b"RIFF"


def test_elevenlabs_ogg_voice_note_when_configured(app, monkeypatch):
    ogg = b"OggS fake opus payload"
    monkeypatch.setenv("MAYA_TTS_PROVIDER", "elevenlabs")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")
    monkeypatch.setenv("MAYA_TTS_ELEVENLABS_VOICE_ID", "voice-1")
    calls = []
    async def post_bytes(u, headers=None, json=None, timeout=None):
        calls.append((u, headers, json))
        return 200, ogg
    app.http.post_bytes = post_bytes
    data, ext, mime = run(app._tts("namaste"))
    assert (data, ext, mime) == (ogg, "ogg", "audio/ogg")
    u, headers, body = calls[0]
    assert "text-to-speech/voice-1" in u and "opus_48000_64" in u
    assert headers["xi-api-key"] == "test-key" and body["model_id"] == "eleven_multilingual_v2" and body["text"] == "namaste"
    # /voice command delivers it as a Telegram voice note
    run(app._tg_process("ok", "/voice hello", 42))
    f = [k for m, _, k in app.http.calls if m == "FILE" and "sendVoice" in _]
    assert f and f[0]["filename"] == "maya-voice.ogg" and f[0]["data"] == ogg and f[0]["field"] == "voice"


def test_elevenlabs_failure_falls_back_to_gemini(app, monkeypatch):
    import base64
    pcm = b"\x00\x01" * 100
    monkeypatch.setenv("MAYA_TTS_PROVIDER", "elevenlabs")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")
    monkeypatch.setenv("MAYA_TTS_ELEVENLABS_VOICE_ID", "voice-1")
    async def post_bytes(u, headers=None, json=None, timeout=None):
        return 402, b"payment_required"
    app.http.post_bytes = post_bytes
    async def post(u, **k):
        return Resp(200, json.dumps({"candidates": [{"content": {"parts": [{"inlineData": {"mimeType": "audio/L16; rate=24000; channels=1", "data": base64.b64encode(pcm).decode()}}]}}]}))
    app.http.post = post
    w, ext, _ = run(app._tts("hello")); assert ext == "wav" and w[:4] == b"RIFF"


def _fake_tts_wav(app):
    import base64
    pcm = b"\x00\x01" * 100
    async def post(u, **k):
        app.http.calls.append(("POST", u, k))
        if "3.8-flash-tts" in u: return Resp(429, "{}")
        if "generateContent" in u and "speechConfig" in json.dumps(k.get("json", {})):
            return Resp(200, json.dumps({"candidates": [{"content": {"parts": [{"inlineData": {"mimeType": "audio/L16; rate=24000; channels=1", "data": base64.b64encode(pcm).decode()}}]}}]}))
        return Resp(200, json.dumps({"candidates": [{"content": {"parts": [{"text": "pong"}]}}]}))
    app.http.post = post
    return pcm


def test_voice_mode_commands(app):
    run(app._tg_process("ok", "/voice", 42))
    assert app.state.core.voice_mode is True
    msgs = [k["json"]["text"] for m, u, k in app.http.calls if "sendMessage" in u]
    assert any("Voice mode ON" in t for t in msgs)
    run(app._tg_process("ok", "/voice status", 42))
    msgs = [k["json"]["text"] for m, u, k in app.http.calls if "sendMessage" in u]
    assert any("Voice mode ON hai" in t for t in msgs)
    run(app._tg_process("ok", "/voice off", 42))
    assert app.state.core.voice_mode is False
    msgs = [k["json"]["text"] for m, u, k in app.http.calls if "sendMessage" in u]
    assert any("Voice mode OFF" in t for t in msgs)


def test_voice_mode_speaks_answers_voice_only(app):
    pcm = _fake_tts_wav(app)
    run(app._tg_process("ok", "/voice on", 42))
    n0 = len(app.http.calls)
    run(app._tg_process("ok", "hi", 42))
    new = app.http.calls[n0:]
    assert not any("sendMessage" in u and k["json"]["text"] == "pong" for m, u, k in new)  # NO duplicate text in voice mode
    f = [k for m, u, k in new if m == "FILE" and "sendAudio" in u]
    assert f and f[0]["data"][:4] == b"RIFF" and pcm in f[0]["data"]  # the ANSWER is spoken, not the input "hi"
    tts = [k["json"] for m, u, k in new if "generateContent" in u and "speechConfig" in json.dumps(k.get("json", {}))]
    assert tts and tts[0]["contents"][0]["parts"][0]["text"] == "pong"


def test_voice_mode_tts_failure_falls_back_to_text(app):
    run(app._tg_process("ok", "/voice on", 42))
    async def post(u, **k):
        app.http.calls.append(("POST", u, k))
        if "generateContent" in u and "speechConfig" in json.dumps(k.get("json", {})):
            return Resp(429, "{}")  # both TTS models fail
        return Resp(200, json.dumps({"candidates": [{"content": {"parts": [{"text": "pong"}]}}]}))
    app.http.post = post
    run(app._tg_process("ok", "hi", 42))
    msgs = [k["json"]["text"] for m, u, k in app.http.calls if "sendMessage" in u]
    assert "pong" in msgs  # answer never silently lost
    assert any("Awaaz nahi ban payi" in t for t in msgs)  # honest fallback note
    assert not [k for m, u, k in app.http.calls if m == "FILE"]


def test_voice_mode_off_no_audio(app):
    _fake_tts_wav(app)
    run(app._tg_process("ok", "hi", 42))
    assert not [k for m, u, k in app.http.calls if m == "FILE"]
    assert any("sendMessage" in u for m, u, k in app.http.calls)


def test_voice_mode_low_budget_refuses_cleanly_no_doomed_calls(app):
    import time as _t
    _fake_tts_wav(app)
    run(app._tg_process("ok", "/voice on", 42))
    app._job_start = _t.monotonic() - (app.PROCESS_BUDGET_S - 2)  # <3s stage budget: calls refuse instead of dying mid-flight
    run(app._tg_process("ok", "hi", 42))
    msgs = [k["json"]["text"] for m, u, k in app.http.calls if "sendMessage" in u]
    assert any("could not get an answer" in t for t in msgs)  # honest failure, not a silent loss
    assert not [k for m, u, k in app.http.calls if m == "FILE"]  # no doomed TTS/upload attempt


def test_outer_guard_recovers_generated_answer_as_text(app):
    """Actual-deadline semantics (scaled): the answer WAS generated, the voice send outlives the guard,
    the timeout branch delivers the answer as text - once, no resend message, no duplicate."""
    import time as _t
    run(app._tg_process("ok", "/voice on", 42))
    async def post(u, **k):
        app.http.calls.append(("POST", u, k))
        body = json.dumps(k.get("json", {}))
        if "speechConfig" in body:
            await asyncio.sleep(30)  # TTS hangs - outlives the job guard
            return Resp(200, "{}")
        return Resp(200, json.dumps({"candidates": [{"content": {"parts": [{"text": "pong"}]}}]}))
    app.http.post = post
    app.BUDGET_S = 0.5  # scale the real guard down; PROCESS_BUDGET stays, so stages may start
    try:
        run(app._tg_guarded("ok", "hi", 42))
    finally:
        app.BUDGET_S = 24
    msgs = [k["json"]["text"] for m, u, k in app.http.calls if "sendMessage" in u]
    assert msgs.count("pong") == 1  # the generated answer was recovered and delivered as text, exactly once
    assert not any("took too long" in t for t in msgs)  # no loss message when the answer actually landed
    assert not [k for m, u, k in app.http.calls if m == "FILE"]  # the hung audio send was cancelled


def test_outer_guard_timeout_without_answer_sends_resend(app):
    """Nothing generated yet (chat stage hangs): the honest resend message is the only possible outcome."""
    async def post(u, **k):
        app.http.calls.append(("POST", u, k))
        if "generateContent" in u:
            await asyncio.sleep(30)
            return Resp(200, "{}")
        return Resp(200, "{}")
    app.http.post = post
    app.BUDGET_S = 0.5
    try:
        run(app._tg_guarded("ok", "hi", 42))
    finally:
        app.BUDGET_S = 24
    msgs = [k["json"]["text"] for m, u, k in app.http.calls if "sendMessage" in u]
    assert any("took too long" in t for t in msgs) and "pong" not in msgs


def test_clamped_timeout_only_inside_job(app):
    import time as _t
    assert app._clamped_timeout(9) == 9  # outside a job: unchanged default
    app._job_start = _t.monotonic()
    assert 2.0 <= app._clamped_timeout(9) <= 9
    app._job_start = _t.monotonic() - app.BUDGET_S
    assert app._clamped_timeout(9) is None  # budget spent: caller skips the call


def test_voice_note_answer_spoken_in_mode(app):
    pcm = _fake_tts_wav(app)
    app.core_bound = True
    async def get_bytes(u, timeout=None):
        return 200, b"OggS fake audio"
    app.http.get_bytes = get_bytes
    async def post(u, **k):
        app.http.calls.append(("POST", u, k))
        body = json.dumps(k.get("json", {}))
        if "getFile" in u: return Resp(200, json.dumps({"result": {"file_path": "voice/file_1.oga"}}))
        if "Transcribe this voice message" in body:
            return Resp(200, json.dumps({"candidates": [{"content": {"parts": [{"text": "aaj mausam kaisa hai"}]}}]}))
        if "speechConfig" in body:
            import base64
            return Resp(200, json.dumps({"candidates": [{"content": {"parts": [{"inlineData": {"mimeType": "audio/L16; rate=24000; channels=1", "data": base64.b64encode(pcm).decode()}}]}}]}))
        if "generateContent" in u: return Resp(200, json.dumps({"candidates": [{"content": {"parts": [{"text": "pong"}]}}]}))
        return Resp(200, "{}")
    app.http.post = post
    async def get(u, **k):
        if "getFile" in u: return Resp(200, json.dumps({"result": {"file_path": "voice/file_1.oga"}}))
        return Resp(200, "[]")
    app.http.get = get
    run(app._tg_process("ok", "/voice on", 42))
    n0 = len(app.http.calls)
    run(app._tg_process("voice", json.dumps({"file_id": "f1", "duration": 3, "size": 1000, "mime": "audio/ogg"}), 42))
    new = app.http.calls[n0:]
    f = [k for m, u, k in new if m == "FILE" and "sendAudio" in u]
    assert f and f[0]["data"][:4] == b"RIFF"  # understood via STT, answer spoken in mode
    assert not any("sendMessage" in u and k["json"]["text"] == "pong" for m, u, k in new)  # voice-only, no text copy


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


def test_mail_gh_commands_honest_and_guarded(app):
    for txt in ["/mail", "/mail send to bob@x.com: hi", "/gh edit whoss4hill/maya-app main a.kt: x", "/gh read whoss4hill/maya-app dev a.kt"]:
        run(app._tg_process("ok", txt, 42))
    msgs = [k["json"]["text"] for _, u, k in app.http.calls if "sendMessage" in u]
    assert "connected nahi" in msgs[-4] and "NOT sent" in msgs[-3] and "protected_branch" in msgs[-2] and "connected nahi" in msgs[-1]
    assert not any("generateContent" in u or "gmail" in u or "api.github" in u for _, u, _ in app.http.calls)


def test_live_train_request_gets_clear_unsupported_reply_no_gemini(app):
    run(app._tg_process("ok", "Mujhe aaj ara patna City jana h 1labhi se 10 baje ke bich passenger train dekho.", 42))
    assert not any("generateContent" in u for _, u, _ in app.http.calls)
    assert "live" in [k["json"]["text"] for _, u, k in app.http.calls if "sendMessage" in u][-1]
    run(app._tg_process("ok", "python mein list kaise banate hain", 42))
    assert any("generateContent" in u for _, u, _ in app.http.calls)

def test_webhook_defers_processing_and_returns_fast(app):
    H = {"X-Telegram-Bot-Api-Secret-Token": "s3"}
    code = run(app.handle("POST", "/v1/telegram/pair/start", A, ""))[1]["code"]
    def tg(i, text): return run(app.handle("POST", "/telegram/webhook", H, json.dumps({"update_id": i, "message": {"from": {"id": 42}, "chat": {"id": 42, "type": "private"}, "text": text, "date": int(time.time())}})))
    tg(1, f"/pair {code}")
    jobs = []
    app.defer = lambda c: jobs.append(c)
    n = len(app.http.calls); st = tg(2, "hello")
    assert st[0] == 200 and len(app.http.calls) == n and len(jobs) == 1
    run(jobs[0])
    assert any("generateContent" in u for _, u, _ in app.http.calls)


def test_job_state_done_timeout_and_stale_ack(app):
    run(app._tg_guarded("ok", "hello", 42)); run(app._job("done"))
    assert app.state.job["state"] == "done"
    async def slow(*a, **k): await asyncio.sleep(1)
    app._tg_process = slow; app.BUDGET_S = 0.05
    run(app._tg_guarded("ok", "hello", 42))
    assert app.state.job["state"] == "timeout"
    assert "took too long" in [k["json"]["text"] for _, u, k in app.http.calls if "sendMessage" in u][-1]

def test_gemini_call_exception_falls_to_next_model(app):
    calls = []
    orig = app.http.post
    async def post(url, **k):
        calls.append(url)
        if len(calls) == 1: raise asyncio.TimeoutError()
        return await orig(url, **k)
    app.http.post = post
    out = run(app._gemini("hi"))
    assert len(calls) >= 2 and out


def test_voice_intent_exact_words_and_dispatch(app):
    from edge import voice_intent
    assert voice_intent("Ak vn send kro jisme hello golu mere jaan kaise ho kya kr rahe ho bolo") == "hello golu mere jaan kaise ho kya kr rahe ho"
    assert voice_intent("voice note bhejo: good morning") == "good morning"
    assert voice_intent("ek audio bana do jisme tum bolo happy birthday") == "tum bolo happy birthday"
    assert voice_intent("vn bhejo jisme hello golu ki jaan kaise ho bolo") == "hello golu ki jaan kaise ho"
    assert voice_intent("voice note bhejo good morning send kro") == "good morning send kro"
    assert voice_intent("vn send kro") is None
    assert voice_intent("voice assistant kya hota hai") is None
    assert voice_intent("/voice hi") is None and voice_intent("hello") is None

def test_vn_request_goes_to_tts_not_gemini_chat(app, monkeypatch):
    sent = []
    async def fake_tts(text): sent.append(text); return None
    app._tts = fake_tts
    run(app._tg_process("ok", "Ak vn send kro jisme hello golu mere jaan kaise ho kya kr rahe ho bolo", 42))
    assert sent == ["hello golu mere jaan kaise ho kya kr rahe ho"]
    assert not any("generateContent" in u for _, u, _ in app.http.calls)

def test_live_token_mint(app):
    st, body = run(app.handle("POST", "/v1/live/token", A, "{}"))
    assert st == 200, body
    assert body["token"].startswith("auth_tokens/")
    assert body["model"] == "models/gemini-3.8-live"
    assert body["ws_url"].endswith(body["token"])
    assert "BidiGenerateContentConstrained" in body["ws_url"] and "v1alpha" in body["ws_url"]
    calls = [c for c in app.http.calls if "auth_tokens" in c[1]]
    assert len(calls) == 1
    m, url, k = calls[0]
    assert m == "POST" and "v1beta/auth_tokens" in url
    assert k["headers"]["x-goog-api-key"] == "g"
    b = k["json"]
    assert b["uses"] == 1 and b["expireTime"].endswith("Z")
    setup = b["bidiGenerateContentSetup"]
    assert setup["model"] == "models/gemini-3.8-live"
    assert setup["generationConfig"]["responseModalities"] == ["AUDIO"]
    assert "sessionResumption" in setup
    assert "systemInstruction" not in setup and "speechConfig" not in setup.get("generationConfig", {})

def test_live_token_auth_required(app):
    assert run(app.handle("POST", "/v1/live/token", {}, "{}"))[0] == 401
    assert not [c for c in app.http.calls if "auth_tokens" in c[1]]

def test_live_token_upstream_failure_honest(app):
    async def fail(url, **k):
        if "auth_tokens" in url: return Resp(429, "{}")
        return await FakeHttp._r(app.http, "POST", url, **k)
    app.http.post = fail
    st, body = run(app.handle("POST", "/v1/live/token", A, "{}"))
    assert st == 502 and body["error"] == "live_token_upstream"
    assert "token" not in body

def test_live_token_model_env_override(app, monkeypatch):
    monkeypatch.setenv("MAYA_LIVE_MODEL", "models/gemini-2.5-flash-native-audio-preview-12-2025")
    st, body = run(app.handle("POST", "/v1/live/token", A, "{}"))
    assert st == 200 and body["model"].endswith("12-2025")

def test_live_token_rate_limited(app):
    for _ in range(30):
        assert run(app.handle("POST", "/v1/live/token", A, "{}"))[0] == 200
    st, body = run(app.handle("POST", "/v1/live/token", A, "{}"))
    assert st == 429 and body["bucket"] == "write"


def test_pdf_ai_failure_recorded_and_honest(app):
    import asyncio as _aio
    H = {"X-Telegram-Bot-Api-Secret-Token": "s3"}
    code = run(app.handle("POST", "/v1/telegram/pair/start", A, ""))[1]["code"]
    def tg(i, text): return run(app.handle("POST", "/telegram/webhook", H, json.dumps({"update_id": i, "message": {"from": {"id": 42}, "chat": {"id": 42, "type": "private"}, "text": text, "date": int(time.time())}})))
    tg(1, f"/pair {code}")
    app.http.raise_on_gen = True
    tg(2, "/pdf Python notes")
    msgs = [k["json"]["text"] for _, u, k in app.http.calls if "sendMessage" in u]
    assert any("Could not generate the document" in m and "AI error timeout" in m for m in msgs)
    assert not [1 for m, _, _ in app.http.calls if m == "FILE"]  # fail-closed: no document sent
    assert any(f["stage"] == "pdf_model" for f in app.state.core.fail_get())


def test_pdf_prompt_bounded_length(app):
    run(app._tg_process("ok", "/pdf notes", 42))
    payloads = [k["json"] for _, u, k in app.http.calls if "generateContent" in u]
    assert any("700 words" in p["contents"][-1]["parts"][0]["text"] for p in payloads)


def test_pdf_send_failure_recorded(app):
    H = {"X-Telegram-Bot-Api-Secret-Token": "s3"}
    code = run(app.handle("POST", "/v1/telegram/pair/start", A, ""))[1]["code"]
    def tg(i, text): return run(app.handle("POST", "/telegram/webhook", H, json.dumps({"update_id": i, "message": {"from": {"id": 42}, "chat": {"id": 42, "type": "private"}, "text": text, "date": int(time.time())}})))
    tg(1, f"/pair {code}")
    app.http.file_status = 500
    tg(2, "/pdf notes")
    msgs = [k["json"]["text"] for _, u, k in app.http.calls if "sendMessage" in u]
    assert any("PDF could not be sent." in m for m in msgs)
    assert any(f["stage"] == "pdf_send" and "code=500" in f["status"] for f in app.state.core.fail_get())


def test_pdf_send_ok_false_recorded(app):
    H = {"X-Telegram-Bot-Api-Secret-Token": "s3"}
    code = run(app.handle("POST", "/v1/telegram/pair/start", A, ""))[1]["code"]
    def tg(i, text): return run(app.handle("POST", "/telegram/webhook", H, json.dumps({"update_id": i, "message": {"from": {"id": 42}, "chat": {"id": 42, "type": "private"}, "text": text, "date": int(time.time())}})))
    tg(1, f"/pair {code}")
    app.http.file_ok = False  # HTTP 200 but Telegram says the document was not accepted
    tg(2, "/pdf notes")
    msgs = [k["json"]["text"] for _, u, k in app.http.calls if "sendMessage" in u]
    assert any("PDF could not be sent." in m for m in msgs)
    assert any(f["stage"] == "pdf_send" and "ok=false" in f["status"] for f in app.state.core.fail_get())


def test_pdf_timeout_rescue_delivers_finished_document(app):
    import asyncio as _aio
    async def slow(status, text, chat_id):
        app._pending_pdf = ("PDF: notes", "notes.pdf", b"%PDF-1.4 fake")
        raise _aio.TimeoutError()
    app._tg_process = slow
    run(app._tg_guarded("ok", "/pdf notes", 42))
    files = [k for m, _, k in app.http.calls if m == "FILE"]
    assert len(files) == 1 and files[0]["fields"]["caption"] == "PDF: notes" and files[0]["data"] == b"%PDF-1.4 fake"
    msgs = [k["json"]["text"] for _, u, k in app.http.calls if "sendMessage" in u]
    assert not any("took too long" in m for m in msgs)


def test_pdf_prompt_bounded_no_preamble(app):
    run(app._tg_process("ok", "/pdf notes", 42))
    payloads = [k["json"] for _, u, k in app.http.calls if "generateContent" in u]
    assert any("700 words" in p["contents"][-1]["parts"][0]["text"] and "no preamble" in p["contents"][-1]["parts"][0]["text"] for p in payloads)
    assert app._pending_pdf is None  # marker cleared after a completed send: no duplicate rescue later
