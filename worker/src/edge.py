"""Plain-Python router for the Cloudflare Worker (no FastAPI/pydantic import: they cost ~20 ms CPU per cold start,
over the Free plan's 10 ms limit). Same behaviour as main.py for: /health, /v1/chat, /v1/memories, /v1/tasks, /v1/live/token, Telegram.
Search is not included (OFF by design). The /auth/reset page is not ported yet (main.py still serves it on Render).
handle() is pure logic over an injected Http and state, so it is testable without Cloudflare."""
import re
import asyncio, json, os, re, time

from net import Http
from state_core import WORKER_MODELS, OPENROUTER_MODELS, GROQ_MODELS
import gmail_tools, github_tools, secret_guard
from pdfgen import make_pdf, unsupported_chars, UnsupportedText


def _env(k, d=""):
    return os.environ.get(k, d)


_LIVE = re.compile(r"\b(train|passenger|express|memu|pnr|platform|running status|live status|flight|bus timing|ticket)\b|\b(ट्रेन)\b", re.I)
_LIVE_ASK = re.compile(r"\b(dekho|dekh|check|batao|bata|status|time|timing|kab|kitne baje|search|find|dhundh|book)\b", re.I)


_VN_WORD = re.compile(r"\b(vn|v\.n\.?|voice\s*note|voice\s*message|voice|audio)\b", re.I)
_VN_ASK = re.compile(r"\b(send|bhej\w*|bana\w*|kro|karo|kar\s*do|sun\w*|bol\w*|record)\b", re.I)
_VN_DELIM = re.compile(r"\b(jisme|jismein|jis\s+mein)\b|:")
_VN_LEAD = re.compile(r"^\s*((ak|ek|ik)\s+)?(vn|voice\s*note|voice\s*message|voice|audio)\b\s*((send|bhejo|bhej\s*do|bhej|bana\s*do|banao|bana|kro|karo|kar\s*do)\b\s*)*", re.I)
_VN_TAIL = re.compile(r"\s+(bolo|bol\s*do|bolna|bolke|bol\s*ke|keh\s*do)\s*[.!]*\s*$", re.I)


def voice_intent(text):
    """Natural 'send a VN saying <words>' -> the exact words to speak, else None. Only the unambiguous command frame is removed:
    everything after 'jisme'/':' stays verbatim except one trailing command marker (bolo/bol do...). Without a delimiter only the
    leading 'vn send kro' words are removed and the rest is kept untouched."""
    t = (text or "").strip().split("She replied")[0].strip()
    if t.startswith("/") or not (_VN_WORD.search(t) and _VN_ASK.search(t)):
        return None
    m = _VN_DELIM.search(t)
    if m:
        spoken = _VN_TAIL.sub("", t[m.end():].strip()).strip()
    else:
        m2 = _VN_LEAD.match(t)
        spoken = t[m2.end():].strip() if m2 and m2.end() > 0 else ""
    return spoken if len(spoken) >= 2 else None


_IMG_VERB = re.compile(r"(?i)\b(?:bana(?:o|do|ke|na)?|banaye|generate|create|make|draw|paint|design|dikha(?:o|do)? ?ek)\b")
_IMG_NOUN = re.compile(r"(?i)\b(?:image|images|picture|pic|tasveer|tasvir|drawing|illustration|artwork)\b")
_IMG_NOT = re.compile(r"(?i)\b(?:send|forward|share|find|search|open|delete|show me my|my (?:photos|pictures|images)|gallery|screenshot|photo of me|edit (?:this|my)|frame|frames|size|resolution|format|file|wallpaper|translate|explain|meaning|summari[sz]e|rewrite|correct|quote|said|says|told|wrote|message|text|caption|prompt|how (?:do|to|can)|what|why|which)\b")
_IMG_QUOTED = re.compile(r"[\"\u201c\u201d`]")


def image_intent(text):
    """Conservative natural-language image request. Returns the drawing request text or None."""
    t = (text or "").strip()
    if not t or len(t) > 400 or t.startswith("/"):
        return None
    if _IMG_QUOTED.search(t) or t.endswith("?"):
        return None
    if _IMG_VERB.search(t) and _IMG_NOUN.search(t) and not _IMG_NOT.search(t):
        return t
    return None


def _needs_live_lookup(text):
    t = text or ""
    return bool(_LIVE.search(t) and _LIVE_ASK.search(t))


class App:
    def __init__(self, http=None, state=None):
        self.http = http or Http(timeout=30.0)
        self.state = state
        self.url = _env("SUPABASE_URL").rstrip("/")
        self.anon = _env("SUPABASE_ANON_KEY").strip()
        self.gem_key = _env("GEMINI_API_KEY")
        self.model = _env("GEMINI_MODEL", "gemini-3.6-flash")
        self.owner = _env("MAYA_OWNER_USER_ID").strip().lower()
        self.tg_token = _env("TELEGRAM_BOT_TOKEN")
        if not self.url or not self.anon or not self.gem_key:
            raise RuntimeError("missing SUPABASE_URL / SUPABASE_ANON_KEY / GEMINI_API_KEY")
        if _env("SUPABASE_SERVICE_ROLE_KEY"):
            raise RuntimeError("service_role key must not be set on the Worker")

    # ---- helpers
    @staticmethod
    def err(status, code):
        return status, {"error": code}

    def _sb(self, token):
        return {"apikey": self.anon, "Authorization": f"Bearer {token}"}

    async def user(self, headers):
        h = headers.get("authorization", "")
        token = h[7:] if h.startswith("Bearer ") else None
        if not token:
            return None, None, self.err(401, "missing_token")
        r = await self.http.get(f"{self.url}/auth/v1/user", headers=self._sb(token))
        if r.status_code != 200:
            return None, None, self.err(401, "invalid_token")
        u = r.json()
        if not u or not u.get("id"):
            return None, None, self.err(401, "invalid_token")
        if not self.owner or str(u["id"]).strip().lower() != self.owner:
            return None, None, self.err(403, "not_authorized")
        return u, token, None

    def _gem(self, prompt, history=None):
        import datetime
        today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        rule = ("You are Maya, Sahil's personal assistant, chatting with him on Telegram. This is an ongoing conversation: never introduce yourself "
                "or say Namaste/hello again unless he greets you first or asks who you are. Answer directly and briefly in natural Roman Hinglish "
                "(match his language; use English or other content languages only when he asks for them, e.g. code or English text). "
                "Never end with generic 'how can I help' lines. You know nothing about him beyond this chat. "
                "Today's date is " + today + " (UTC). You have NO internet, browsing, search or other tools in this chat. "
                "You CAN make audio: when he asks for a voice note or audio of some words, the app turns the exact words into an audio file sent to this chat; never say you cannot record or send voice (to speak exact words: '/voice <exact words>'; for an ongoing voice conversation where every answer is also spoken: '/voice on', stop with '/voice off'). You CAN make PDFs with /pdf. "
                "Never claim to have checked a website, searched, or verified anything live. For anything that may have changed "
                "(latest versions, news, prices, dates), say plainly that you cannot check live and that your knowledge may be outdated; "
                "give any answer only as 'from memory, may be outdated' and never invent links or release dates.")
        return {"system_instruction": {"parts": [{"text": rule}]}, "contents": [{"role": r, "parts": [{"text": t}]} for r, t in (history or [])] + [{"role": "user", "parts": [{"text": prompt}]}]}

    TTS_MODELS = ("gemini-3.8-flash-tts", "gemini-3.1-flash-tts-preview")

    async def _tts_elevenlabs(self, text):
        """Explicitly configured ElevenLabs TTS (owner's own key, premade voice, multilingual v2).
        Returns (ogg_opus_bytes, "ogg", "audio/ogg") or None on any failure. Active ONLY when
        MAYA_TTS_PROVIDER=elevenlabs + ELEVENLABS_API_KEY + MAYA_TTS_ELEVENLABS_VOICE_ID are all set;
        otherwise, or on failure, the Gemini chain below stays the default."""
        import os as _os
        if _os.environ.get("MAYA_TTS_PROVIDER", "").strip().lower() != "elevenlabs":
            return None
        key = _os.environ.get("ELEVENLABS_API_KEY", "")
        vid = _os.environ.get("MAYA_TTS_ELEVENLABS_VOICE_ID", "")
        if not key or not vid:
            print("elevenlabs tts configured off: missing key or voice id")
            return None
        try:
            status, raw = await self.http.post_bytes(
                "https://api.elevenlabs.io/v1/text-to-speech/" + vid + "?output_format=opus_48000_64",
                headers={"xi-api-key": key}, timeout=self._clamped_timeout(12) or 12,
                json={"text": str(text)[:1500], "model_id": "eleven_multilingual_v2"})
            print("elevenlabs tts status", status)
            if status == 200 and raw:
                return raw, "ogg", "audio/ogg"
        except Exception as ex:
            print("elevenlabs tts error", type(ex).__name__)
        return None

    async def _tts(self, text):
        """Returns (audio_bytes, ext, mime) or None. ElevenLabs only when explicitly configured;
        default is the free-tier Gemini TTS chain -> WAV. Output file format depends on the provider used."""
        import base64, io, wave
        el = await self._tts_elevenlabs(text)
        if el is not None:
            return el
        for model in self.TTS_MODELS:
            to = self._clamped_timeout(9)
            if to is None:
                print("tts skipped: job budget spent")
                return None
            r = await self.http.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers={"Content-Type": "application/json", "x-goog-api-key": self.gem_key}, timeout=to,
                json={"contents": [{"parts": [{"text": text}]}], "generationConfig": {"responseModalities": ["AUDIO"],
                      "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": "Kore"}}}}})
            self.last_gemini_status = r.status_code
            print("tts", model, "status", r.status_code)
            if r.status_code != 200:
                continue
            try:
                inl = r.json()["candidates"][0]["content"]["parts"][0]["inlineData"]
                raw = base64.b64decode(inl["data"])
                if raw[:4] == b"RIFF":
                    return raw, "wav", "audio/wav"
                rate = 24000
                for part in inl.get("mimeType", "").split(";"):
                    if part.strip().startswith("rate="):
                        rate = int(part.strip()[5:])
                buf = io.BytesIO()
                with wave.open(buf, "wb") as w:
                    w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(raw)
                return buf.getvalue(), "wav", "audio/wav"
            except Exception as ex:
                print("tts parse error", type(ex).__name__)
        return None

    FALLBACK_MODELS = ("gemini-3.7-flash", "gemini-3.1-flash-lite", "gemini-3-flash-preview", "gemini-flash-lite-latest", "gemini-3.8-flash")

    async def _gemini(self, prompt, history=None):
        """Free-tier quotas are per model and small (primary showed limit 20/day): on 429/404 try the next model.
        503/500 are retried once after a short wait. Still free tier only: no billing, no search."""
        models = [self.model] + [m for m in self.FALLBACK_MODELS if m != self.model]
        fell_from = None
        try:
            pref = await self.state.model_pref_get()
            if pref == "backup_only":
                return await self._backup_text(prompt, chosen=True)
            if pref in WORKER_MODELS:
                return await self._backup_text(prompt, chosen=True, model=pref)
            if pref in OPENROUTER_MODELS or pref in GROQ_MODELS:
                out = await self._public_pool_text(prompt, pref)
                if out:
                    return out
                # safe fallback direction only: public pool -> private-tolerant Gemini chain (never the reverse)
                fell_from = (OPENROUTER_MODELS.get(pref) or GROQ_MODELS.get(pref) or "The selected model").split(" (")[0]
        except Exception:
            pass
        r = None
        for model in models:
            for attempt in range(2):
                to = self._clamped_timeout(9)
                if to is None:
                    print("gemini skipped: job budget spent")
                    return None
                try:
                    r = await self.http.post(
                        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                        headers={"Content-Type": "application/json", "x-goog-api-key": self.gem_key}, json=self._gem(prompt, history), timeout=to)
                except Exception as ex:
                    print("gemini", model, "call failed", type(ex).__name__)
                    self.last_gemini_status = "timeout"
                    r = None
                    break
                self.last_gemini_status = r.status_code
                print("gemini", model, "status", r.status_code, "chars", len(r.text))
                if r.status_code in (500, 503) and attempt == 0:
                    await asyncio.sleep(2)
                    continue
                break
            if r is None:
                continue
            if r.status_code == 200:
                try:
                    parts = r.json()["candidates"][0]["content"]["parts"]
                    text = "".join(p.get("text", "") for p in parts).strip() or "..."
                except Exception:
                    text = "..."
                if fell_from:
                    text += "\n\n(" + fell_from + " was unavailable, so Maya's default model answered.)"
                return text
            if r.status_code not in (429, 404, 500, 503):
                break
        return await self._backup_text(prompt, fell_from=fell_from)

    async def _backup_text(self, prompt, chosen=False, model=None, fell_from=None):
        """Same-account Cloudflare Workers AI fallback (Free allocation only; the platform refuses past it)."""
        ai = getattr(self, "ai", None)
        if ai is None or not hasattr(ai, "text"):
            return None
        out = await (ai.text(str(prompt)[:6000], model=model) if model else ai.text(str(prompt)[:6000]))
        if not out:
            return None
        self.used_backup = True
        if chosen and model and model in WORKER_MODELS:
            note = "(" + WORKER_MODELS[model] + " answered, as selected in settings.)"
        elif fell_from:
            note = "(" + fell_from + " and the default model were unavailable, so the backup model answered.)"
        else:
            note = "(Backup AI model answered, as selected in settings.)" if chosen else "(Backup AI model answered because the main one was unavailable.)"
        return out.strip() + "\n\n" + note

    async def _gen_image(self, prompt, image_bytes=None):
        """Returns (b64, engine_label, used_fallback). Tries the user's chosen engine,
        then the fallback; labels always name the ACTUAL engine that produced the image.
        image_bytes (optional) = owner-supplied source photo for an EDIT request."""
        from state_core import DEFAULT_IMAGE_MODEL, FALLBACK_IMAGE_MODEL, IMAGE_ENGINE_NAMES
        first = await self.ai.image(prompt, model=DEFAULT_IMAGE_MODEL, image_bytes=image_bytes)
        if first:
            return first, IMAGE_ENGINE_NAMES[DEFAULT_IMAGE_MODEL], False
        if image_bytes:
            # edit request: the fallback engine accepts no input image, so falling back would
            # silently return a FRESH generation that ignores the owner's photo. Fail honestly.
            await self._fail("image_edit_engine", getattr(self.ai, "last_image_error", None) or "none")
            return None, None, False
        second = await self.ai.image(prompt, model=FALLBACK_IMAGE_MODEL, image_bytes=image_bytes)
        if second:
            return second, IMAGE_ENGINE_NAMES[FALLBACK_IMAGE_MODEL], True
        await self._fail("image_gen_engine", getattr(self.ai, "last_image_error", None) or "none")
        return None, None, False

    async def _public_pool_text(self, prompt, pref):
        """Public free pools (OpenRouter / Groq): public data only. Sends ONLY the current prompt - no history,
        no memories, no screen content, no voice transcripts. Secret-looking prompts are refused before leaving.
        Failure returns None so the caller falls back to the Gemini chain (safe direction only)."""
        import os as _os
        if pref in OPENROUTER_MODELS:
            base, env, models, tag = "https://openrouter.ai/api/v1/chat/completions", "OPENROUTER_API_KEY", OPENROUTER_MODELS, "openrouter"
        else:
            base, env, models, tag = "https://api.groq.com/openai/v1/chat/completions", "GROQ_API_KEY", GROQ_MODELS, "groq"
        key = _os.environ.get(env, "")
        if not key:
            return None  # key not configured yet; silent absence, honest fallback
        if secret_guard.looks_like_secret(str(prompt)):
            return "I can't send that to the free public model pool - it looks private. Ask me without the private part, or switch the model back in settings."
        model_id = pref.split("/", 1)[1]  # strip the routing prefix
        try:
            r = await self.http.post(base,
                headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
                json={"model": model_id, "messages": [{"role": "user", "content": str(prompt)[:6000]}], "max_tokens": 600})
            print(tag, model_id, "status", r.status_code)  # status only - never the prompt
            if r.status_code != 200:
                return None
            out = r.json()["choices"][0]["message"]["content"]
        except Exception as ex:
            print(tag, "error", type(ex).__name__)
            return None
        if not out:
            return None
        return out.strip() + "\n\n(" + models.get(pref, "Free public model") + " answered.)"

    # Maya's own per-minute burst limits - NOT provider quotas. Defends against loops/abuse burning the
    # shared free pools; a human never legitimately exceeds these. Configurable per env, defaults below.
    RL_DEFAULTS = {"chat": 20, "image": 3, "write": 30, "read": 60}

    def _rl_bucket(self, method, path):
        if path == "/v1/chat" and method == "POST":
            return "chat"
        if path == "/v1/image" and method == "POST":
            return "image"
        if method == "GET":
            return "read"
        return "write"

    async def _rl(self, method, path):
        bucket = self._rl_bucket(method, path)
        try:
            limit = int(os.environ.get("MAYA_RL_" + bucket.upper() + "_PER_MIN") or self.RL_DEFAULTS[bucket])
            if limit < 1:
                limit = self.RL_DEFAULTS[bucket]
        except Exception:
            limit = self.RL_DEFAULTS[bucket]
        try:
            allowed, retry = await self.state.rl_hit(bucket, limit)
        except Exception:
            return self.err(503, "rate_counter_unavailable")
        if not allowed:
            return 429, {"error": "maya_rate_limited", "bucket": bucket, "retry_after_s": retry,
                         "note": "Maya's own per-minute burst limit (not a provider quota). Wait a few seconds and retry."}
        return None

    # ---- router
    async def handle(self, method, path, headers, body):
        headers = {k.lower(): v for k, v in (headers or {}).items()}
        if body and len(body) > 32000 and path.startswith("/v1/"):
            return self.err(413, "payload_too_large")
        try:
            data = json.loads(body) if body else {}
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        if path == "/health" and method == "GET":
            return 200, {"ok": True, "service": "maya-backend-edge"}
        if path == "/telegram/webhook" and method == "POST":
            return await self._tg_webhook(headers, data)
        if path.startswith("/v1/"):
            u, tok, e = await self.user(headers)
            if e:
                return e
            limited = await self._rl(method, path)
            if limited:
                return limited
            return await self._v1(method, path, u, tok, data, media=headers.get("x-maya-media") == "1")
        return self.err(404, "not_found")

    async def _v1(self, method, path, u, tok, data, media=False):
        sb, uid = self._sb(tok), u["id"]
        rest = f"{self.url}/rest/v1"
        if path == "/v1/chat" and method == "POST":
            msg = str(data.get("message") or "").strip()
            if not msg:
                return self.err(400, "message_required")
            if secret_guard.looks_like_secret(msg):
                return 200, {"reply": secret_guard.NOTICE}  # not stored, not sent to the model
            if media and getattr(self, "ai", None) is not None:
                ip = image_intent(msg)
                if ip is not None:  # opt-in clients only: they then call /v1/image; installed clients never see this field
                    return 200, {"reply": "Ek minute, tasveer bana rahi hoon.", "image_prompt": ip[:400]}
            r = await self.http.get(f"{rest}/memories", headers=sb, params={
                "select": "kind,content", "user_id": f"eq.{uid}", "order": "last_used_at.desc.nullslast", "limit": "20"})
            mem = r.json() if r.status_code == 200 else []
            block = "\n".join(f"- ({m.get('kind')}) {m.get('content')}" for m in mem)
            prompt = ("You are Maya, the owner's personal agent.\n"
                      + (f"What you remember about the owner:\n{block}\n\n" if block else "") + f"Owner says: {msg}")
            reply = await self._gemini(prompt)
            if reply is None:
                return self.err(502, "gemini_unavailable")
            ins = await self.http.post(f"{rest}/conversations", headers={**sb, "Content-Type": "application/json"}, json=[
                {"user_id": uid, "role": "user", "text": msg}, {"user_id": uid, "role": "maya", "text": reply}])
            if ins.status_code >= 400:
                return self.err(500, "internal_error")
            return 200, {"reply": reply}
        if path == "/v1/image" and method == "POST":
            prompt = str(data.get("prompt") or "").strip()
            if not prompt or len(prompt) > 400:
                return self.err(400, "prompt_required_max_400_chars")
            if secret_guard.looks_like_secret(prompt):
                return self.err(422, "secret_detected")
            ai = getattr(self, "ai", None)
            if ai is None:
                return self.err(501, "image_not_configured")
            img_in = None
            b64in = data.get("image_b64")
            if isinstance(b64in, str) and b64in:
                import base64 as _b64
                try:
                    img_in = _b64.b64decode(b64in)
                    if len(img_in) > 4 * 1024 * 1024:
                        img_in = None
                except Exception:
                    img_in = None
            img, engine, used_fb = await self._gen_image(prompt, image_bytes=img_in)
            if not img:
                # root cause not established: engine error OR free daily allowance - never claim one
                return self.err(502, "image_unavailable")
            return 200, {"image_base64": img, "mime": "image/jpeg", "model": engine, "fallback": used_fb}
        if path == "/v1/memories" and method == "GET":
            r = await self.http.get(f"{rest}/memories", headers=sb, params={
                "select": "id,kind,content,source,created_at,last_used_at", "user_id": f"eq.{uid}", "order": "created_at.desc"})
            return (200, {"memories": r.json()}) if r.status_code < 400 else self.err(500, "internal_error")
        if path == "/v1/memories" and method == "POST":
            kind, content = data.get("kind"), data.get("content")
            if not kind or not content:
                return self.err(400, "kind_and_content_required")
            if secret_guard.looks_like_secret(str(content)):
                return self.err(422, "secret_detected")
            r = await self.http.post(f"{rest}/memories", headers={**sb, "Content-Type": "application/json", "Prefer": "return=representation"},
                                     params={"select": "id"}, json={"user_id": uid, "kind": kind, "content": content, "source": data.get("source") or "chat"})
            return (201, {"id": r.json()[0]["id"]}) if r.status_code < 400 else self.err(500, "internal_error")
        m = re.fullmatch(r"/v1/memories/([A-Za-z0-9-]{1,64})", path)
        if m and method == "DELETE":
            r = await self.http.delete(f"{rest}/memories", headers=sb, params={"id": f"eq.{m.group(1)}", "user_id": f"eq.{uid}"})
            return (204, None) if r.status_code < 400 else self.err(500, "internal_error")
        if path == "/v1/tasks" and method == "GET":
            r = await self.http.get(f"{rest}/tasks", headers=sb, params={
                "select": "id,title,status,due,notes,created_at,updated_at", "user_id": f"eq.{uid}", "order": "created_at.desc"})
            return (200, {"tasks": r.json()}) if r.status_code < 400 else self.err(500, "internal_error")
        if path == "/v1/tasks" and method == "POST":
            if not data.get("title"):
                return self.err(400, "title_required")
            r = await self.http.post(f"{rest}/tasks", headers={**sb, "Content-Type": "application/json", "Prefer": "return=representation"},
                                     params={"select": "id"}, json={"user_id": uid, "title": data["title"], "due": data.get("due") or None, "notes": data.get("notes") or None})
            return (201, {"id": r.json()[0]["id"]}) if r.status_code < 400 else self.err(500, "internal_error")
        if path == "/v1/settings/model" and method in ("GET", "POST"):
            if method == "POST":
                v = data.get("model") if isinstance(data, dict) else None
                if v not in ("auto", "backup_only") and v not in WORKER_MODELS and v not in OPENROUTER_MODELS and v not in GROQ_MODELS:
                    return self.err(422, "invalid_model")
                try:
                    await self.state.model_pref_set(v)
                except Exception:
                    return self.err(422, "invalid_model")
            return 200, {"model": await self.state.model_pref_get(),
                         "options": ["auto", "backup_only"] + list(WORKER_MODELS) + list(OPENROUTER_MODELS) + list(GROQ_MODELS),
                         "labels": {"auto": "Maya chooses (Gemini first, backup if busy)", "backup_only": "Backup model only", **WORKER_MODELS, **OPENROUTER_MODELS, **GROQ_MODELS}}
        if path == "/v1/live/token" and method == "POST":
            return await self._live_token()
        if path == "/v1/telegram/status" and method == "GET":
            return 200, await self.state.status()
        if path == "/v1/telegram/pair/start" and method == "POST":
            tid = data.get("chat_id")
            if tid is not None and (isinstance(tid, bool) or not isinstance(tid, int) or tid <= 0):
                return self.err(400, "bad_chat_id")
            return 200, {"code": await self.state.pair_start(tid), "expires_in": 600}
        if path == "/v1/telegram/toggle" and method == "POST":
            if not isinstance(data.get("on"), bool):
                return self.err(400, "on_required")
            await self.state.set_enabled(data["on"])
            return 200, {"enabled": data["on"]}
        if path == "/v1/telegram/selftest" and method == "POST":
            st = await self.state.status()
            bid = await self.state.bound()
            if not bid or not data.get("text"):
                return self.err(400, "not_bound_or_no_text")
            await self._tg_process("ok", str(data["text"])[:2000], bid)
            return 200, {"ran": True}
        if path == "/v1/telegram/revoke" and method == "POST":
            await self.state.revoke()
            return 200, {"revoked": True}
        return self.err(404, "not_found")


    LIVE_TOKEN_TTL_MIN = 30
    LIVE_WS = "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1alpha.GenerativeService.BidiGenerateContentConstrained?access_token="

    async def _live_token(self):
        """Mint a single-use ephemeral token so the app can open a client-direct Gemini Live session.
        Audio never passes through this backend; the token is locked to the configured live model,
        AUDIO output and session resumption. No token value is ever logged or stored."""
        model = _env("MAYA_LIVE_MODEL", "models/gemini-3.8-live")
        if not re.fullmatch(r"models/[A-Za-z0-9._-]{1,60}", model):
            return self.err(500, "live_model_invalid")
        import datetime
        exp = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=self.LIVE_TOKEN_TTL_MIN)).strftime("%Y-%m-%dT%H:%M:%SZ")
        body = {"uses": 1, "expireTime": exp,
                "bidiGenerateContentSetup": {"model": model,
                                             "generationConfig": {"responseModalities": ["AUDIO"]},
                                             "sessionResumption": {}}}
        try:
            r = await self.http.post("https://generativelanguage.googleapis.com/v1beta/auth_tokens",
                                     headers={"x-goog-api-key": self.gem_key, "Content-Type": "application/json"},
                                     json=body)
            tok = r.json().get("name", "") if r.status_code == 200 else ""
        except Exception:
            return self.err(502, "live_token_upstream")
        if not tok.startswith("auth_tokens/"):
            return self.err(502, "live_token_upstream")
        return 200, {"token": tok, "expire_time": exp, "model": model, "ws_url": self.LIVE_WS + tok,
                     "note": "Single use; connect within 1 minute of mint. Audio flows directly between the app and Google, never through this backend."}

    async def _tg_send(self, chat_id, text):
        r = await self.http.post(f"https://api.telegram.org/bot{self.tg_token}/sendMessage",
                                 json={"chat_id": chat_id, "text": text[:4000]}, timeout=self._clamped_timeout(6) or 6)
        if getattr(r, "status_code", 0) >= 400:
            await self._fail("tg_send", "status " + str(r.status_code))
        return r

    @staticmethod
    def chunks(text, size=3900, max_chunks=10):
        out, rest = [], text
        while rest and len(out) < max_chunks:
            if len(rest) <= size:
                out.append(rest); rest = ""; break
            cut = rest.rfind("\n", 0, size)
            cut = cut if cut > size // 2 else size
            out.append(rest[:cut]); rest = rest[cut:].lstrip("\n")
        if rest:
            out[-1] = out[-1][: size - 40] + "\n[truncated: reply too long]"
        return out

    async def _tg_send_long(self, chat_id, text):
        last = None
        for c in self.chunks(text):
            last = await self._tg_send(chat_id, c)
            if last.status_code >= 400:
                break
        return last

    async def _tg_webhook(self, headers, update):
        status, text, chat_id = await self.state.update(headers, update)
        print("tg update status", status)
        if not (self.tg_token and isinstance(chat_id, int)):
            return 200, {}
        # Answer Telegram at once: slow model calls must not hold the webhook open (Telegram times out at ~60 s,
        # reports "Read timeout expired", and the update id is already consumed, so the message would be lost silently).
        try:
            prev = await self.state.job_get() if hasattr(self.state, "job_get") else None
            if prev and prev.get("state") == "processing" and time.time() - prev.get("ts", 0) > 60 and status == "ok":
                await self._tg_send(chat_id, "My previous reply did not finish. If you did not get it, please send it again.")
        except Exception as ex:
            print("stale job check error", type(ex).__name__)
        await self._job("processing", status, text, alarm_s=45)
        if getattr(self, "defer", None):
            self.defer(self._tg_guarded(status, text, chat_id))
        else:
            await self._tg_guarded(status, text, chat_id)
        return 200, {}

    BUDGET_S = 24  # Workers keep background work alive ~30 s after the response
    PROCESS_BUDGET_S = 21  # stages stop here; the last ~3 s are reserved for delivering the answer

    async def _job(self, state, status="", text="", alarm_s=0):
        """Durable job record in the Durable Object: processing -> done | error | timeout (never silent loss)."""
        try:
            if hasattr(self.state, "job_set"):
                await self.state.job_set(state, str(text)[:60], alarm_s)
        except Exception as ex:
            print("job_set error", type(ex).__name__)

    async def _tg_guarded(self, status, text, chat_id):
        self._job_start = time.monotonic()
        self._pending = None
        try:
            await asyncio.wait_for(self._tg_process(status, text, chat_id), self.BUDGET_S)
            await self._job("done")
            print("tg job secs", round(time.monotonic() - self._job_start, 1))
        except asyncio.TimeoutError:
            await self._job("timeout")
            print("tg budget exceeded")
            # If the answer was already generated but the voice send ate the budget, deliver it as text now
            # (reserved seconds after the guard) instead of losing it. Marker is cleared before sending: no duplicates.
            pend, self._pending = self._pending, None
            delivered = False
            if pend:
                try:
                    r = await self._tg_send_long(chat_id, pend)
                    delivered = r is not None and r.status_code < 400
                    print("timeout recovery send", r.status_code if r else "?")
                except Exception as ex:
                    print("timeout recovery error", type(ex).__name__)
            if not delivered:
                try:
                    await self._fail("tg_job", "timeout " + str(status))
                    await self._tg_send(chat_id, "That took too long and I could not finish it. Please send it again in a bit.")
                except Exception:
                    pass
        except Exception as ex:
            await self._job("error")
            print("tg guarded error", type(ex).__name__)


    VOICE_MAX_SECONDS = 120  # product bound: short voice notes only
    VOICE_MAX_BYTES = 10 * 1024 * 1024  # well inside Gemini's inline-request limit and Telegram's 20 MB bot download cap
    VOICE_MIMES = {"audio/ogg", "audio/mpeg", "audio/wav", "audio/x-wav", "audio/mp4", "audio/x-m4a"}

    async def _stt(self, data, mime):
        """Free-tier Gemini audio transcription -> transcript text. "" = no speech heard, None = provider error."""
        import base64
        b64 = base64.b64encode(data).decode()
        models = [self.model] + [m for m in self.FALLBACK_MODELS if m != self.model]
        for model in models:
            to = self._clamped_timeout(10)
            if to is None:
                print("stt skipped: job budget spent")
                return None
            r = await self.http.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers={"Content-Type": "application/json", "x-goog-api-key": self.gem_key}, timeout=to,
                json={"contents": [{"parts": [
                    {"inlineData": {"mimeType": mime, "data": b64}},
                    {"text": "Transcribe this voice message exactly as spoken, in the original language. "
                             "If there is no speech or it is unintelligible, reply exactly NO_SPEECH."}]}]})
            print("stt", model, "status", r.status_code)
            if r.status_code != 200:
                continue
            try:
                txt = "".join(p.get("text", "") for p in r.json()["candidates"][0]["content"]["parts"]).strip()
            except Exception as ex:
                print("stt parse error", type(ex).__name__)
                return None
            if not txt or txt.upper().startswith("NO_SPEECH"):
                return ""
            return txt[:2000]
        return None

    async def _fail(self, stage, status):
        """Metadata-only failure record (stage + status, never content). Bounded ring in the DO;
        readable via the owner-JWT status route. Never raises."""
        try:
            await self.state.fail_add(stage, status)
        except Exception:
            pass

    async def _photo(self, chat_id, meta_json):
        """Owner photo: only an EDIT request when image mode is ON; otherwise a hint.
        Runs under the watchdog like chat; fallback engine schnell does not accept an input
        image, so edits only run on the primary engine."""
        import json as _json
        try:
            meta = _json.loads(meta_json)
        except Exception:
            meta = {}
        if not await self.state.image_mode_get():
            await self._tg_send(chat_id, "Photo mil gayi. Photo edit karne ke liye pehle image mode chalu karo: /image on (band karne ke liye /image off).")
            return
        if getattr(self, "ai", None) is None:
            await self._tg_send(chat_id, "Image editing is not set up on this server.")
            return
        file_id = meta.get("file_id")
        caption = (meta.get("caption") or "").strip()
        if not file_id:
            if meta.get("no_small_variant"):
                await self._fail("photo_edit", "no_small_variant")
                await self._tg_send(chat_id, "Is photo ka koi chhota variant nahi mila jo editing ke liye safe ho (edit engine sirf 512px se chhoti input leta hai). Is photo ko main edit nahi kar paungi.")
            return
        try:
            r = await self.http.get(f"https://api.telegram.org/bot{self.tg_token}/getFile", params={"file_id": file_id}, timeout=self._clamped_timeout(6) or 6)
            fpath = (r.json().get("result") or {}).get("file_path") if r.status_code == 200 else None
        except Exception as ex:
            fpath = None
            print("photo getFile error", type(ex).__name__)
        if not fpath:
            await self._fail("photo_edit", "getFile empty")
            await self._tg_send(chat_id, "I could not download that photo - please send it again.")
            return
        try:
            st, img = await self.http.get_bytes(f"https://api.telegram.org/file/bot{self.tg_token}/{fpath}", timeout=self._clamped_timeout(10) or 10)
            if st != 200:
                img = b""
        except Exception as ex:
            img = b""
            print("photo download error", type(ex).__name__)
        if not img or len(img) > 20 * 1024 * 1024:
            await self._fail("photo_edit", "download failed")
            await self._tg_send(chat_id, "I could not download that photo - please send it again.")
            return
        prompt = caption or "enhance this photo"
        b64, engine, used_fb = await self._gen_image(prompt, image_bytes=img)
        if not b64:
            await self._fail("photo_edit", "engine none " + str(getattr(self, "last_gemini_status", "?")))
            await self._tg_send(chat_id, "I could not edit the photo right now. The free daily image allowance may be used up or the service is busy. Nothing was charged. Please try again later.")
            return
        import base64
        jpg = base64.b64decode(b64)
        cap = (prompt[:180]) + ("\n(edited with " + engine + ")" if engine else "")
        r = await self.http.post_file(f"https://api.telegram.org/bot{self.tg_token}/sendPhoto",
                                      {"chat_id": chat_id, "caption": cap}, "maya-edited.jpg", jpg, mime="image/jpeg", field="photo")
        print("tg_sendPhoto(edit) status", r.status_code)
        if r.status_code >= 400:
            r = await self.http.post_file(f"https://api.telegram.org/bot{self.tg_token}/sendDocument",
                                          {"chat_id": chat_id, "caption": cap}, "maya-edited.jpg", jpg, mime="image/jpeg")
            print("tg_sendDocument(edit) status", r.status_code)
            if r.status_code >= 400:
                await self._tg_send(chat_id, "The edited photo was made but could not be sent.")

    async def _voice_note(self, chat_id, meta_json):
        """Owner voice note -> Telegram file download -> Gemini STT -> normal chat reply. Never silently ignored:
        every failure path sends the owner a clear ack."""
        try:
            meta = json.loads(meta_json or "{}")
        except Exception:
            meta = {}
        fid = meta.get("file_id")
        dur, size = int(meta.get("duration") or 0), int(meta.get("size") or 0)
        mime = str(meta.get("mime") or "audio/ogg")
        print("voice note secs", dur, "bytes", size, "mime", mime)  # metadata only - never the transcript or audio
        if not fid:
            return
        if dur > self.VOICE_MAX_SECONDS or (size and size > self.VOICE_MAX_BYTES):
            await self._fail("voice_note", "too_long")
            await self._tg_send(chat_id, "That voice note is too long for me (about 2 minutes max). Please send a shorter one or type it.")
            return
        if mime not in self.VOICE_MIMES:
            await self._fail("voice_note", "bad_mime " + mime)
            await self._tg_send(chat_id, "I could not read that audio format. A normal voice note works best - or just type it.")
            return
        try:
            r = await self.http.get(f"https://api.telegram.org/bot{self.tg_token}/getFile", params={"file_id": fid}, timeout=self._clamped_timeout(6) or 6)
            fp = (r.json().get("result") or {}).get("file_path") if r.status_code == 200 else None
        except Exception as ex:
            fp = None
            print("voice getFile error", type(ex).__name__)
        if not fp:
            await self._fail("voice_note", "getFile empty")
            await self._tg_send(chat_id, "I could not download that voice note - please send it again or type it.")
            return
        try:
            st, data = await self.http.get_bytes(f"https://api.telegram.org/file/bot{self.tg_token}/{fp}", timeout=self._clamped_timeout(10) or 10)
            if st != 200:
                data = b""
        except Exception as ex:
            data = b""
            print("voice download error", type(ex).__name__)
        if not data or len(data) > 20 * 1024 * 1024:  # Telegram bot download hard cap
            await self._fail("voice_note", "download failed" + (" oversized" if data else ""))
            await self._tg_send(chat_id, "I could not download that voice note - please send it again or type it.")
            return
        if mime in ("audio/wav", "audio/x-wav") and self._wav_is_silence(data):
            await self._tg_send(chat_id, "I could not hear anything in that voice note - please try again or type it.")
            return
        try:
            transcript = await self._stt(data, mime)
        except Exception as ex:
            transcript = None
            print("stt error", type(ex).__name__)
        if transcript is None:
            await self._fail("voice_stt", "none " + str(getattr(self, "last_gemini_status", "?")))
            await self._tg_send(chat_id, "I could not make out that voice note right now - please try again or type it.")
            return
        if not transcript:
            await self._fail("voice_stt", "empty_transcript")
            await self._tg_send(chat_id, "I could not hear any words in that voice note - please try again or type it.")
            return
        # The transcript is untrusted recognition, not typed intent: plain chat answers are fine, but
        # commands/actions (/image, /pdf, /voice, natural image or voice requests) need typed confirmation.
        t = transcript.strip()
        if t.startswith("/") or voice_intent(t) or image_intent(t):
            await self._tg_send(chat_id, 'I heard: "' + t[:300] + '". If that is right, send it to me as a typed message and I will do it - I never act on commands from audio on my own.')
            return
        await self._tg_process("ok", transcript, chat_id)

    @staticmethod
    def _wav_is_silence(data):
        """True only for (near-)digital-silence PCM WAV we can decode ourselves. Compressed formats
        (OGG/Opus, mp3) return False - codec bitrate is not reliable speech detection."""
        import io, struct, wave
        try:
            with wave.open(io.BytesIO(data), "rb") as w:
                frames = w.readframes(w.getnframes())
                sw = w.getsampwidth()
            if sw != 2 or not frames:
                return False
            n = len(frames) // 2
            vals = struct.unpack("<%dh" % n, frames[: n * 2])
            rms = (sum(v * v for v in vals) / n) ** 0.5
            return rms < 50  # 16-bit PCM: digital silence ~0, quiet room noise <50, real speech is far above
        except Exception:
            return False

    async def _send_tts_out(self, chat_id, out):
        """Send a (data, ext, mime) TTS result to the owner: real voice note for OGG/Opus, audio file (with document fallback) for WAV."""
        data, ext, mime = out
        if ext == "ogg":
            r = await self.http.post_file(f"https://api.telegram.org/bot{self.tg_token}/sendVoice",
                                          {"chat_id": chat_id}, "maya-voice.ogg", data, mime=mime, field="voice")
            print("tg_sendVoice status", r.status_code)
            return r.status_code < 400
        r = await self.http.post_file(f"https://api.telegram.org/bot{self.tg_token}/sendAudio",
                                      {"chat_id": chat_id, "title": "Maya voice"}, "maya-voice." + ext, data, mime=mime, field="audio")
        print("tg_sendAudio status", r.status_code)
        if r.status_code >= 400:
            r = await self.http.post_file(f"https://api.telegram.org/bot{self.tg_token}/sendDocument",
                                          {"chat_id": chat_id}, "maya-voice." + ext, data, mime=mime)
            print("tg_sendDocument status", r.status_code)
        return r.status_code < 400

    async def _voice_mode_on(self):
        fn = getattr(self.state, "voice_mode_get", None)
        if fn is None:
            return False
        try:
            return bool(await fn())
        except Exception as ex:
            print("voice_mode read error", type(ex).__name__)
            return False

    def _left(self):
        """Seconds of wall budget left in this webhook job, or None outside a guarded job."""
        start = getattr(self, "_job_start", None)
        return None if start is None else max(0.0, self.PROCESS_BUDGET_S - (time.monotonic() - start))

    def _clamped_timeout(self, default):
        """Per-call timeout capped by the remaining job budget; None = no usable time, skip the call."""
        left = self._left()
        if left is None:
            return default
        return max(2.0, min(default, left - 1.0)) if left > 3.0 else None

    async def _action(self, chat_id, action):
        try:
            r = await self.http.post(f"https://api.telegram.org/bot{self.tg_token}/sendChatAction", json={"chat_id": chat_id, "action": action})
            print("tg chatAction", action, r.status_code)
        except Exception:
            pass

    async def _heartbeat(self, chat_id, action):
        for _ in range(12):  # at most about 48 s, one refresh every 4 s (Telegram shows the action for 5 s)
            await asyncio.sleep(4)
            await self._action(chat_id, action)

    async def _tg_process(self, status, text, chat_id):
        if status == "voice":
            await self._voice_note(chat_id, text)
            return
        if status == "ok" and text and not text.lstrip().startswith("/"):
            spoken = voice_intent(text)
            if spoken:
                text = "/voice " + spoken  # natural request -> the real voice tool (WAV audio file to the owner's own chat)
        if status == "ok" and text and not text.lower().startswith(("/image", "/video")):
            action = "upload_document" if text.lower().startswith("/pdf") and text[4:].strip() else ("record_voice" if text.lower().startswith("/voice") and text[6:].strip() else "typing")
            await self._action(chat_id, action)
            hb = None
            try:
                hb = asyncio.ensure_future(self._heartbeat(chat_id, action))
            except Exception:
                hb = None
            try:
                await self._tg_process_inner(status, text, chat_id)
            finally:
                if hb is not None:
                    hb.cancel()
        else:
            await self._tg_process_inner(status, text, chat_id)

    def _tool_reply(self, text):
        """/mail and /gh: validate with the scoped logic; nothing is sent or pushed here. Honest status until credentials exist."""
        if text.lower().startswith("/mail"):
            c = gmail_tools.parse_command(text)
            if c is None or c["op"] == "error":
                return "Mail command samajh nahi aaya. Use: /mail, /mail from <naam>, /mail search <query>, /mail send to <email>: <text>"
            if c["op"] == "draft":
                return gmail_tools.draft_preview(c) + "\n\n(Gmail abhi connected nahi hai, isliye bhej nahi sakti.)"
            return "Gmail abhi connected nahi hai. Pehle Gmail access setup hona baaki hai, mail nahi padh payi."
        c = github_tools.parse_command(text)
        if c is None or c["op"] == "error":
            why = (c or {}).get("why", "format")
            return "GitHub command refused (" + why + "). Use: /gh read <repo> <branch> <path> ya /gh edit <repo> <branch> <path>: <kya badalna hai>. Sirf maya repos, main branch pe kabhi nahi."
        if not os.environ.get("MAYA_GITHUB_ENABLED"):
            return "GitHub access abhi connected nahi hai. Command valid hai, par kuch padha/badla nahi."
        return github_tools.plan_preview(c)

    async def _tg_process_inner(self, status, text, chat_id):
        try:
            if status == "paired":
                await self._tg_send(chat_id, "Paired. Maya is now connected to this chat only.")
            elif status == "photo" and text:
                await self._photo(chat_id, text)
            elif status == "ok" and text and secret_guard.looks_like_secret(text):
                await self._tg_send(chat_id, secret_guard.NOTICE + " You may also delete that message from this chat.")
            elif status == "ok" and text:
                low = text.lower()
                if low.startswith("/pdf"):
                    topic = text[4:].strip()
                    if unsupported_chars(topic):
                        await self._tg_send(chat_id, "PDF supports English/Latin text only for now. Hindi and other scripts need a font that is not added yet, so I did not create a PDF.")
                    elif not topic:
                        await self._tg_send(chat_id, "Usage: /pdf <what the document should contain>")
                    else:
                        body = await self._gemini("Write a clear, well organised plain-text document (no markdown symbols) for: " + topic)
                        if not body:
                            await self._tg_send(chat_id, "Could not generate the document right now (AI error " + str(getattr(self, "last_gemini_status", "?")) + "). Please try again in a minute.")
                        else:
                            try:
                                pdf = make_pdf(topic[:80], body)
                            except UnsupportedText:
                                await self._tg_send(chat_id, "The text contains characters the PDF font cannot show (for example Hindi). No PDF was created.")
                                return 200, {}
                            r = await self.http.post_file(f"https://api.telegram.org/bot{self.tg_token}/sendDocument",
                                                          {"chat_id": chat_id, "caption": "PDF: " + topic[:100]}, (re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")[:40] or "document") + ".pdf", pdf)
                            print("tg_sendDocument status", r.status_code)
                            if r.status_code >= 400:
                                await self._tg_send(chat_id, "PDF could not be sent.")
                elif re.match(r"^/?call\s+\S", low) and hasattr(self.state, "push_command"):
                    target = re.sub(r"^/?call\s+", "", text, flags=re.I).strip()[:80]
                    import uuid
                    cid = uuid.uuid4().hex[:12]
                    res = await self.state.push_command({"id": cid, "cmd": "call", "target": target})
                    if res == "sent":
                        await self._tg_send(chat_id, "Sent to the phone (id " + cid + "). Waiting for the phone to confirm; nothing is confirmed until it replies.")
                    else:
                        await self._tg_send(chat_id, "Phone app is not connected right now. Nothing was dialled and nothing is queued.")
                elif low.startswith("/voice"):
                    vt = text[6:].strip()
                    ctl = vt.lower()
                    if not vt or ctl in ("on", "off", "status"):
                        # Voice conversation mode: every normal answer is ALSO spoken in Maya's voice (text still sent).
                        set_fn = getattr(self.state, "voice_mode_set", None)
                        if ctl == "status" or set_fn is None:
                            on = await self._voice_mode_on()
                            await self._tg_send(chat_id, "Voice mode " + ("ON hai. Har jawab sirf awaaz me jata hai." if on else "OFF hai.") + " Chalu: /voice on, band: /voice off, ya kisi exact text ko bolwane ke liye /voice <text>.")
                        else:
                            on = ctl != "off"
                            await set_fn(on)
                            if on:
                                await self._tg_send(chat_id, "Voice mode ON. Ab jo bhi bhejoge - text ya voice note - main samajhkar jawab dungi aur woh sirf awaaz me sunai dega. Band karne ke liye /voice off.")
                            else:
                                await self._tg_send(chat_id, "Voice mode OFF. Ab jawab sirf text me jayenge.")
                    elif len(vt) > 1500:
                        await self._tg_send(chat_id, "Text is too long for one voice clip (max 1500 characters).")
                    else:
                        out = await self._tts(vt)
                        if not out:
                            await self._fail("tts", "none " + str(getattr(self, "last_gemini_status", "?")))
                            await self._tg_send(chat_id, "Could not generate the audio right now (AI error " + str(getattr(self, "last_gemini_status", "?")) + ").")
                        elif not await self._send_tts_out(chat_id, out):
                            await self._tg_send(chat_id, "Audio could not be sent.")
                elif low.startswith("/image"):
                    raw = text[6:].strip()
                    rawlow = raw.lower()
                    if rawlow in ("", "on", "off", "status"):
                        on = (rawlow in ("", "on"))  # plain /image enters persistent image mode
                        if rawlow == "status":
                            cur = await self.state.image_mode_get()
                            await self._tg_send(chat_id, "Image mode " + ("ON - photo bhejo to main usse edit karung" + "i" + ", text se tasveer banayengi. Band karne ke liye /image off." if cur else "OFF - photos par edit nahi hota. Chalu karne ke liye /image on."))
                        else:
                            await self.state.image_mode_set(on)
                            if on:
                                await self._tg_send(chat_id, "Image mode ON. Photo bhejo (caption me kya karna hai likho) to main edit karke wapas bhejung" + "i" + ". Ya text me bolo kya banao. Band karne ke liye /image off.")
                            else:
                                await self._tg_send(chat_id, "Image mode OFF. Ab photos par edit nahi hoga. /image <kya banana hai> se aise bhi tasveer ban sakti hai.")
                    else:
                        ip = raw
                        if not ip or len(ip) > 400:
                            await self._tg_send(chat_id, "Usage: /image <what to draw> (up to 400 characters). Sirf /image bhejne se image mode ON hota hai, /image off se band.")
                        elif getattr(self, "ai", None) is None:
                            await self._tg_send(chat_id, "Image generation is not set up on this server.")
                        else:
                            b64, engine, used_fb = await self._gen_image(ip)
                            caption = ip[:200] + ("\n(made with " + engine + " - the usual image engine was unavailable)" if used_fb else "")
                            if not b64:
                                await self._tg_send(chat_id, "I could not make the image right now. The free daily image allowance may be used up or the service is busy. Nothing was charged. Please try again later.")
                            else:
                                import base64
                                jpg = base64.b64decode(b64)
                                r = await self.http.post_file(f"https://api.telegram.org/bot{self.tg_token}/sendPhoto",
                                                              {"chat_id": chat_id, "caption": caption}, "maya-image.jpg", jpg, mime="image/jpeg", field="photo")
                                print("tg_sendPhoto status", r.status_code)
                                if r.status_code >= 400:
                                    r = await self.http.post_file(f"https://api.telegram.org/bot{self.tg_token}/sendDocument",
                                                                  {"chat_id": chat_id, "caption": caption}, "maya-image.jpg", jpg, mime="image/jpeg")
                                    print("tg_sendDocument(jpg) status", r.status_code)
                                    if r.status_code >= 400:
                                        await self._tg_send(chat_id, "The image was made but could not be sent.")
                elif image_intent(text) is not None and getattr(self, "ai", None) is not None:
                    ip = image_intent(text)
                    if not ip or len(ip) > 400:
                        await self._tg_send(chat_id, "Usage: /image <what to draw> (up to 400 characters).")
                    elif getattr(self, "ai", None) is None:
                        await self._tg_send(chat_id, "Image generation is not set up on this server.")
                    else:
                        b64, engine, used_fb = await self._gen_image(ip)
                        caption = ip[:200] + ("\n(made with " + engine + " - the usual image engine was unavailable)" if used_fb else "")
                        if not b64:
                            await self._tg_send(chat_id, "I could not make the image right now. The free daily image allowance may be used up or the service is busy. Nothing was charged. Please try again later.")
                        else:
                            import base64
                            jpg = base64.b64decode(b64)
                            r = await self.http.post_file(f"https://api.telegram.org/bot{self.tg_token}/sendPhoto",
                                                          {"chat_id": chat_id, "caption": caption}, "maya-image.jpg", jpg, mime="image/jpeg", field="photo")
                            print("tg_sendPhoto status", r.status_code)
                            if r.status_code >= 400:
                                r = await self.http.post_file(f"https://api.telegram.org/bot{self.tg_token}/sendDocument",
                                                              {"chat_id": chat_id, "caption": caption}, "maya-image.jpg", jpg, mime="image/jpeg")
                                print("tg_sendDocument(jpg) status", r.status_code)
                                if r.status_code >= 400:
                                    await self._tg_send(chat_id, "The image was made but could not be sent.")
                elif low.startswith("/video"):
                    await self._tg_send(chat_id, "Video generation is not available. No free video option is set up.")
                elif _needs_live_lookup(text):
                    await self._tg_send(chat_id, "Abhi main live train, flight ya website check nahi kar sakti, to schedule ya status nahi bata sakti. Tum IRCTC ya NTES app dekh lo. Live lookup ab tak bana nahi hai.")
                elif low.startswith("/mail") or low.startswith("/gh"):
                    await self._tg_send(chat_id, self._tool_reply(text))
                else:
                    hist = []
                    try:
                        hist = await self.state.history() if hasattr(self.state, "history") else []
                    except Exception as ex:
                        print("history read error", type(ex).__name__)
                    reply = await self._gemini(text, hist)
                    print("history turns", len(hist) // 2)
                    if reply:
                        try:
                            if hasattr(self.state, "add_turn"):
                                await self.state.add_turn(text, reply)
                        except Exception as ex:
                            print("history write error", type(ex).__name__)
                        left = self._left()
                        if await self._voice_mode_on() and len(reply) <= 1500:
                            # Voice mode: the answer is SPOKEN, not duplicated as text. If TTS or the audio send fails,
                            # the answer is never lost - it falls back to the normal text delivery below.
                            self._pending = reply  # recoverable as text if TTS/audio outlives the job guard
                            if left is not None and left < 4:
                                out = None
                                print("voice_mode tts skipped: low budget")
                            else:
                                out = await self._tts(reply)
                            print("voice_mode tts ok", out is not None)
                            if out is not None and await self._send_tts_out(chat_id, out):
                                self._pending = None
                                return 200, {}
                            await self._tg_send(chat_id, "(Awaaz nahi ban payi, jawab text me:)")
                        r = await self._tg_send_long(chat_id, reply)
                        print("tg sendMessage status", r.status_code, "chunks", len(self.chunks(reply)))
                        if r.status_code >= 400:
                            await self._tg_send(chat_id, "Reply could not be delivered in full. Please try again.")
                    else:
                        await self._fail("chat_model", "none " + str(getattr(self, "last_gemini_status", "?")))
                        await self._tg_send(chat_id, "Maya could not get an answer from the AI right now (error " + str(getattr(self, "last_gemini_status", "?")) + "). Please try again.")
        except Exception as ex:
            print("telegram handler error", type(ex).__name__, str(ex).replace(self.tg_token, "<token>")[:300])
            try:
                await self._tg_send(chat_id, "Something went wrong while handling that. Please try again.")
            except Exception:
                pass
