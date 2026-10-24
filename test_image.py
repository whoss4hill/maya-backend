import json
import test_edge as T
from test_edge import app  # noqa: F401

class FakeAI:
    def __init__(self, out): self.out, self.calls = out, []
    async def image(self, p): self.calls.append(p); return self.out

def post(app, body): return T.run(app.handle("POST", "/v1/image", T.A, json.dumps(body)))

def test_image_ok(app):
    app.ai = FakeAI("QUJD"); s, b = post(app, {"prompt": "a red kite over a hill"})
    assert s == 200 and b["image_base64"] == "QUJD" and b["mime"] == "image/jpeg"

def test_image_not_configured(app):
    s, b = post(app, {"prompt": "a kite"}); assert s == 501

def test_image_refuses_secret_without_calling_ai(app):
    app.ai = FakeAI("QUJD"); s, b = post(app, {"prompt": "draw my password is Hunter22abcd"})
    assert s == 422 and not app.ai.calls

def test_image_validation_and_failure(app):
    app.ai = FakeAI(None)
    assert post(app, {"prompt": ""})[0] == 400
    assert post(app, {"prompt": "x" * 401})[0] == 400
    assert post(app, {"prompt": "a kite"})[0] == 502

def test_image_requires_jwt(app):
    s, b = T.run(app.handle("POST", "/v1/image", {}, json.dumps({"prompt": "a kite"}))); assert s == 401


# ---- Telegram /image and backup text
import base64
from test_edge import Resp

def sent_texts(app):
    return [k["json"]["text"] for m, u, k in app.http.calls if u.endswith("/sendMessage")]

def files(app): return [(u, k) for m, u, k in app.http.calls if m == "FILE"]

def test_tg_image_sends_photo(app):
    app.tg_token = "T"; app.ai = FakeAI(base64.b64encode(b"\xff\xd8jpegbytes").decode())
    T.run(app._tg_process("ok", "/image a red kite", 42))
    f = files(app)
    assert len(f) == 1 and f[0][0].endswith("/sendPhoto") and f[0][1]["data"].startswith(b"\xff\xd8")
    assert f[0][1]["fields"]["chat_id"] == 42

def test_tg_image_unavailable_is_truthful_and_sends_nothing(app):
    app.tg_token = "T"; app.ai = FakeAI(None)
    T.run(app._tg_process("ok", "/image a kite", 42))
    assert not files(app) and "could not make the image" in " ".join(sent_texts(app))

def test_tg_image_secret_prompt_refused_before_ai(app):
    app.tg_token = "T"; app.ai = FakeAI("QUJD")
    T.run(app._tg_process("ok", "/image my password is Hunter22abcd", 42))
    assert not app.ai.calls and not files(app)

def test_tg_image_usage_and_video_honest(app):
    app.tg_token = "T"; app.ai = FakeAI("QUJD")
    T.run(app._tg_process("ok", "/image", 42)); T.run(app._tg_process("ok", "/video a cat", 42))
    t = " ".join(sent_texts(app))
    assert "Usage: /image" in t and "Video generation is not available" in t and not app.ai.calls

class TextAI(FakeAI):
    def __init__(self, out): super().__init__(None); self.tout = out
    async def text(self, p): return self.tout

def _gemini_429(app):
    orig = app.http._r
    async def r(m, url, **k):
        if "generateContent" in url:
            app.http.calls.append((m, url, k)); return Resp(429, "{}")
        return await orig(m, url, **k)
    app.http._r = r

def test_backup_text_used_and_labelled_when_gemini_exhausted(app):
    app.tg_token = "T"; _gemini_429(app); app.ai = TextAI("backup says hi")
    out = T.run(app._gemini("hello"))
    assert out.startswith("backup says hi") and "Backup AI model" in out

def test_no_backup_gives_none(app):
    _gemini_429(app)
    assert T.run(app._gemini("hello")) is None


# ---- natural-language image intent
import pytest
import edge as E

@pytest.mark.parametrize("t", [
    "ek red kite ki image bana do", "draw a picture of a dog on a beach", "generate an image of a mountain at dawn",
    "mere liye ek tasveer banao billi ki", "make an illustration of a robot"])
def test_image_intent_positive(t): assert E.image_intent(t)

@pytest.mark.parametrize("t", [
    "send me my pictures from the gallery", "find the image I saved yesterday", "photo of me edit this",
    "open the picture in my gallery", "make a reminder for 5 pm", "what is an image file format",
    "bana do ek reminder", "forward this image to mom",
    "make a picture frame for my room", "draw a picture of a house costs how much?", 'translate "make an image of a dog" to Hindi',
    'he said "draw a picture of a cat" yesterday', "what size should the image be", "how to make an image smaller",
    "rewrite this: create a picture of my family", "write a caption for the image of my dog", "/image a kite", "delete that picture"])
def test_image_intent_negative(t): assert not E.image_intent(t)

def test_tg_natural_image_request_runs_image(app):
    app.tg_token = "T"; app.ai = FakeAI(base64.b64encode(b"\xff\xd8x").decode())
    T.run(app._tg_process("ok", "ek red kite ki image bana do", 42))
    assert files(app) and files(app)[0][0].endswith("/sendPhoto")

def test_tg_natural_request_without_ai_falls_to_normal_chat(app):
    app.tg_token = "T"
    T.run(app._tg_process("ok", "ek red kite ki image bana do", 42))
    assert not files(app)
