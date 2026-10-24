"""Inbound Telegram voice-note flow: download -> Gemini STT -> chat reply, with clear acks (never silent)."""
import asyncio, json
import pytest
from net import Resp
from state_core import Core

ENV = dict(SUPABASE_URL="https://x.supabase.co", SUPABASE_ANON_KEY="anon", GEMINI_API_KEY="g", MAYA_OWNER_USER_ID="aaa-1",
           MAYA_TELEGRAM_ENABLED="1", MAYA_TELEGRAM_SECRET="s3", TELEGRAM_BOT_TOKEN="tok")
META = json.dumps({"file_id": "V1", "mime": "audio/ogg", "duration": 3, "size": 12000})

class St:
    def __init__(self): self.core = Core()
    async def update(self, h, u): return self.core.telegram_update(h, u)
    async def bound(self): return 42

class FakeHttp:
    transcript = "pong"
    stt_status = 200
    def __init__(self): self.calls = []
    async def get(self, u, **k):
        self.calls.append(("GET", u, k))
        if "getFile" in u: return Resp(200, json.dumps({"ok": True, "result": {"file_path": "voice/file_1.oga"}}))
        return Resp(200, "[]")
    audio = b"FAKEOPUSDATA"
    async def get_bytes(self, u, **k):
        self.calls.append(("GETB", u, k))
        return 200, self.audio
    async def post(self, u, **k):
        self.calls.append(("POST", u, k))
        if "generateContent" in u:
            if self.stt_status != 200: return Resp(self.stt_status, "{}")
            return Resp(200, json.dumps({"candidates": [{"content": {"parts": [{"text": self.transcript}]}}]}))
        return Resp(200, "{}")
    async def delete(self, u, **k): return Resp(200, "{}")

@pytest.fixture
def app(monkeypatch):
    for k, v in ENV.items(): monkeypatch.setenv(k, v)
    import importlib, edge; importlib.reload(edge)
    a = edge.App(http=FakeHttp(), state=St())
    a.defer = None
    return a

def run(c): return asyncio.run(c)
def sent(app): return [k["json"]["text"] for m, u, k in app.http.calls if u.endswith("/sendMessage") for k in [k] if "json" in k and k["json"]]

def test_voice_happy_path_transcribes_and_replies(app):
    run(app._tg_process("voice", META, 42))
    urls = [u for m, u, k in app.http.calls]
    assert any("getFile" in u for u in urls), "must resolve the Telegram file"
    assert any("file/bot" in u for u in urls), "must download the audio"
    assert any("generateContent" in u for u in urls), "must transcribe via Gemini"
    msgs = sent(app)
    assert any("pong" in t for t in msgs), "transcript must flow into the normal chat reply"

def test_voice_too_long_acks_without_download(app):
    run(app._tg_process("voice", json.dumps({"file_id": "V1", "mime": "audio/ogg", "duration": 300, "size": 12000}), 42))
    assert any("too long" in t for t in sent(app))
    assert not any("getFile" in u for m, u, k in app.http.calls)

def _silent_wav():
    import struct
    return b'RIFF'+struct.pack('<I',36+32000)+b'WAVEfmt '+struct.pack('<IHHIIHH',16,1,1,16000,32000,2,16)+b'data'+struct.pack('<I',32000)+bytes(32000)

def test_wav_digital_silence_acks_without_model_call(app):
    app.http.audio = _silent_wav()
    run(app._tg_process("voice", json.dumps({"file_id": "V1", "mime": "audio/wav", "duration": 2, "size": 32044}), 42))
    assert any("could not hear anything" in t for t in sent(app))
    assert not any("generateContent" in u for m, u, k in app.http.calls)

def test_ogg_low_bitrate_not_prejudged(app):
    # compressed audio is never silence-guessed: it always reaches the model
    run(app._tg_process("voice", json.dumps({"file_id": "V1", "mime": "audio/ogg", "duration": 3, "size": 600}), 42))
    assert any("generateContent" in u for m, u, k in app.http.calls)

def test_command_from_audio_never_executes(app):
    app.http.transcript = "/image red kite"
    run(app._tg_process("voice", META, 42))
    msgs = sent(app)
    assert any("I heard" in t and "typed" in t for t in msgs), "must ask for typed confirmation"
    assert not any("images" in u or "generateImage" in u for m, u, k in app.http.calls)

def test_voice_bad_format_acks(app):
    run(app._tg_process("voice", json.dumps({"file_id": "V1", "mime": "video/mp4", "duration": 3, "size": 12000}), 42))
    assert any("format" in t for t in sent(app))

def test_voice_stt_provider_error_acks(app):
    app.http.stt_status = 503
    run(app._tg_process("voice", META, 42))
    assert any("could not make out" in t for t in sent(app))

def test_voice_no_speech_acks(app):
    app.http.transcript = "NO_SPEECH"
    run(app._tg_process("voice", META, 42))
    assert any("could not hear any words" in t for t in sent(app))
