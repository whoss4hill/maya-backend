import json
import test_edge as T
from test_edge import app  # noqa: F401

class FakeAI:
    """out may be a value (returned for every model) or a dict model->value."""
    def __init__(self, out): self.out, self.calls = out, []
    async def image(self, p, model=None, image_bytes=None):
        self.calls.append((p, model, image_bytes))
        return self.out.get(model) if isinstance(self.out, dict) else self.out

def post(app, body): return T.run(app.handle("POST", "/v1/image", T.A, json.dumps(body)))

def test_image_ok(app):
    app.ai = FakeAI("QUJD"); s, b = post(app, {"prompt": "a red kite over a hill"})
    assert s == 200 and b["image_base64"] == "QUJD" and b["mime"] == "image/jpeg"
    assert b["model"] == "FLUX.2 Klein 4B" and b["fallback"] is False

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

def test_tg_image_plain_enables_mode_and_video_honest(app):
    app.tg_token = "T"; app.ai = FakeAI("QUJD")
    T.run(app._tg_process("ok", "/image", 42)); T.run(app._tg_process("ok", "/video a cat", 42))
    t = " ".join(sent_texts(app))
    assert T.run(app.state.image_mode_get()) is True, "plain /image enters persistent image mode"
    assert "Image mode ON" in t and "Video generation is not available" in t and not app.ai.calls
    T.run(app._tg_process("ok", "/image off", 42))
    assert T.run(app.state.image_mode_get()) is False


def test_tg_image_mode_normal_chat_stays_normal(app):
    app.tg_token = "T"
    T.run(app.state.image_mode_set(True))
    T.run(app._tg_process("ok", "aaj mausam kaisa hai?", 42))
    urls = [u for m, u, k in app.http.calls]
    assert any("generateContent" in u for u in urls), "normal text must stay a chat answer in image mode"

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


def test_oversized_v1_body_rejected_before_anything(app):
    big = json.dumps({"message": "a" * 40000})
    s, b = T.run(app.handle("POST", "/v1/chat", T.A, big))
    assert s == 413 and not app.http.calls

def test_normal_body_still_ok(app):
    s, b = T.run(app.handle("POST", "/v1/chat", T.A, json.dumps({"message": "hello"})))
    assert s == 200


def test_model_pref_roundtrip_and_validation(app):
    s, b = T.run(app.handle("GET", "/v1/settings/model", T.A, ""))
    assert s == 200 and b["model"] == "auto"
    s, b = T.run(app.handle("POST", "/v1/settings/model", T.A, json.dumps({"model": "gpt-5"})))
    assert s == 422
    s, b = T.run(app.handle("POST", "/v1/settings/model", T.A, json.dumps({"model": "backup_only"})))
    assert s == 200 and b["model"] == "backup_only"

def test_backup_only_skips_gemini(app):
    T.run(app.handle("POST", "/v1/settings/model", T.A, json.dumps({"model": "backup_only"})))
    s, b = T.run(app.handle("POST", "/v1/chat", T.A, json.dumps({"message": "hello"})))
    assert not any("generateContent" in c[1] for c in app.http.calls)

def test_settings_model_needs_owner(app):
    s, b = T.run(app.handle("GET", "/v1/settings/model", {}, ""))
    assert s in (401, 403)


def test_backup_only_label_does_not_claim_unavailable(app):
    class AI:
        async def text(self, p): return "hello there"
    app.ai = AI()
    T.run(app.handle("POST", "/v1/settings/model", T.A, json.dumps({"model": "backup_only"})))
    s, b = T.run(app.handle("POST", "/v1/chat", T.A, json.dumps({"message": "hi"})))
    r = b["reply"]
    assert "as selected in settings" in r and "unavailable" not in r

def test_auto_label_unchanged(app):
    class AI:
        async def text(self, p): return "x"
    app.ai = AI()
    assert "because the main one was unavailable" in T.run(app._backup_text("q"))


def _with_ai(app):
    class AI:
        async def text(self, p): return "x"
        async def image(self, p): return None
    app.ai = AI()

def test_chat_image_hint_only_for_optin_header(app):
    _with_ai(app)
    msg = json.dumps({"message": "ek billi ki tasveer bana do"})
    h = dict(T.A); h["x-maya-media"] = "1"
    s, b = T.run(app.handle("POST", "/v1/chat", h, msg))
    assert s == 200 and b.get("image_prompt") and set(b) == {"reply", "image_prompt"}
    s, b = T.run(app.handle("POST", "/v1/chat", T.A, msg))
    assert s == 200 and "image_prompt" not in b

def test_optin_header_ordinary_chat_unchanged(app):
    _with_ai(app)
    h = dict(T.A); h["x-maya-media"] = "1"
    s, b = T.run(app.handle("POST", "/v1/chat", h, json.dumps({"message": "kal ka plan batao"})))
    assert s == 200 and "image_prompt" not in b


def test_image_fallback_labels_actual_engine(app):
    from state_core import DEFAULT_IMAGE_MODEL, FALLBACK_IMAGE_MODEL
    app.ai = FakeAI({DEFAULT_IMAGE_MODEL: None, FALLBACK_IMAGE_MODEL: "RkFMTEJBQ0s="})
    s, b = post(app, {"prompt": "a kite"})
    assert s == 200 and b["model"] == "FLUX.1 Schnell" and b["fallback"] is True
    assert [c[1] for c in app.ai.calls] == [DEFAULT_IMAGE_MODEL, FALLBACK_IMAGE_MODEL]

def test_tg_image_fallback_caption_names_actual_engine(app):
    from state_core import DEFAULT_IMAGE_MODEL, FALLBACK_IMAGE_MODEL
    app.ai = FakeAI({DEFAULT_IMAGE_MODEL: None, FALLBACK_IMAGE_MODEL: base64.b64encode(b"jpg").decode()})
    T.run(app._tg_process("ok", "/image a red kite", 42))
    caps = [(k.get("fields") or {}).get("caption", "") for u, k in files(app)]
    assert any("made with FLUX.1 Schnell" in c for c in caps)

def test_tg_image_primary_has_plain_caption(app):
    app.ai = FakeAI(base64.b64encode(b"jpg").decode())
    T.run(app._tg_process("ok", "/image a red kite", 42))
    caps = [(k.get("fields") or {}).get("caption", "") for u, k in files(app)]
    assert caps and all("made with" not in c for c in caps)


def _photo_meta(caption="make the sky orange"):
    return json.dumps({"file_id": "PH1", "caption": caption})


def test_image_mode_command_on_off_status(app):
    T.run(app._tg_process("ok", "/image on", 42))
    assert T.run(app.state.image_mode_get()) is True
    msgs = T.run(app._tg_process("ok", "/image status", 42))  # noqa - reply sent via http fake
    T.run(app._tg_process("ok", "/image off", 42))
    assert T.run(app.state.image_mode_get()) is False


def test_photo_ignored_when_mode_off(app):
    app.ai = FakeAI(base64.b64encode(b"jpg").decode())
    assert T.run(app.state.image_mode_get()) is False
    T.run(app._tg_process("photo", _photo_meta(), 42))
    assert app.ai.calls == []  # no edit without image mode
    urls = [u for m, u, k in app.http.calls]
    assert not any("getFile" in u for u in urls)


def test_photo_edit_when_mode_on(app):
    b64img = base64.b64encode(b"EDITEDJPG").decode()
    app.ai = FakeAI(b64img)
    T.run(app.state.image_mode_set(True))
    T.run(app._tg_process("photo", _photo_meta(), 42))
    urls = [u for m, u, k in app.http.calls]
    assert any("getFile" in u for u in urls), "must resolve the Telegram photo file"
    assert any("file/bot" in u for u in urls), "must download the photo bytes"
    assert app.ai.calls and app.ai.calls[0][2] is not None, "downloaded photo bytes must reach the image engine"
    sent_photos = [k for m, u, k in app.http.calls if u.endswith("/sendPhoto")]
    assert sent_photos, "edited photo must be sent back"


def test_voice_failure_is_recorded_metadata_only(app):
    app.state.core.data["fails"] = []
    app.http.calls.clear()
    # force STT failure: make generateContent return an error
    app.http.stt_status = 500
    T.run(app._tg_process("voice", json.dumps({"file_id": "V1", "mime": "audio/ogg", "duration": 3, "size": 12000}), 42))
    fails = app.state.core.data.get("fails", [])
    assert fails and all(set(f) == {"ts", "stage", "status"} for f in fails), "metadata-only entries"
    assert any(f["stage"] == "voice_stt" for f in fails)


def test_photo_variant_picked_strictly_below_512():
    import telegram_channel as tc
    from state_core import State
    import os
    os.environ.setdefault("MAYA_TELEGRAM_ENABLED", "1"); os.environ.setdefault("MAYA_TELEGRAM_SECRET", "s3")
    upd = {"update_id": 5, "message": {"message_id": 9, "date": 2000000000,
        "from": {"id": 42, "is_bot": False}, "chat": {"id": 42, "type": "private"},
        "caption": "edit it",
        "photo": [
            {"file_id": "small90", "width": 90, "height": 90, "file_size": 1000},
            {"file_id": "mid320", "width": 320, "height": 240, "file_size": 9000},
            {"file_id": "big1280", "width": 1280, "height": 960, "file_size": 90000}]}}
    st = State(); st.owner_enabled = True
    status, payload = tc.handle({"X-Telegram-Bot-Api-Secret-Token": "s3"}, upd, st, now=2000000000.0, owner="42")
    assert status == "photo"
    assert json.loads(payload)["file_id"] == "mid320", "largest variant strictly below 512x512 must be chosen"
