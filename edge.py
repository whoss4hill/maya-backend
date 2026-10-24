"""Plain-Python router for the Cloudflare Worker (no FastAPI/pydantic import: they cost ~20 ms CPU per cold start,
over the Free plan's 10 ms limit). Same behaviour as main.py for: /health, /v1/chat, /v1/memories, /v1/tasks, Telegram.
Search is not included (OFF by design). The /auth/reset page is not ported yet (main.py still serves it on Render).
handle() is pure logic over an injected Http and state, so it is testable without Cloudflare."""
import re
import asyncio, json, os, re, time

from net import Http
import gmail_tools, github_tools
from pdfgen import make_pdf, unsupported_chars, UnsupportedText


def _env(k, d=""):
    return os.environ.get(k, d)


_LIVE = re.compile(r"\b(train|passenger|express|memu|pnr|platform|running status|live status|flight|bus timing|ticket)\b|\b(ट्रेन)\b", re.I)
_LIVE_ASK = re.compile(r"\b(dekho|dekh|check|batao|bata|status|time|timing|kab|kitne baje|search|find|dhundh|book)\b", re.I)


_VN_WORD = re.compile(r"\b(vn|v\.n\.?|voice\s*note|voice\s*message|voice|audio)\b", re.I)
_VN_ASK = re.compile(r"\b(send|bhej\w*|bana\w*|kro|karo|kar\s*do|sun\w*|bol\w*|record)\b", re.I)
_VN_STRIP = re.compile(r"\b(ak|ek|ik)\s+(vn|voice\s*note|voice\s*message|voice|audio)\b|\b(vn|voice\s*note|voice\s*message|voice|audio)\b|\b(send|bhejo|bhej\s*do|bhej|bana\s*do|banao|bana|kro|karo|kar\s*do)\b|\b(jisme|jismein|jis\s*mein|ki)\b", re.I)
_VN_TAIL = re.compile(r"\b(bolo|bol\s*do|bolna|bolke|bol\s*ke|keh\s*do|sunao)\s*[.!]*\s*$", re.I)


def voice_intent(text):
    """Natural Hinglish/English 'send a VN saying <words>' -> the exact words to speak, else None. The words are kept verbatim."""
    t = (text or "").strip()
    if t.startswith("/") or not (_VN_WORD.search(t) and _VN_ASK.search(t)):
        return None
    body = t.split("She replied")[0]
    body = _VN_TAIL.sub("", body).strip()
    spoken = re.sub(r"\s+", " ", _VN_STRIP.sub(" ", body)).strip(" ,.:;-")
    spoken = _VN_TAIL.sub("", spoken).strip(" ,.:;-")
    return spoken if len(spoken) >= 2 else None


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
                "You CAN make audio: when he asks for a voice note or audio of some words, the app turns the exact words into an audio file sent to this chat; never say you cannot record or send voice (if you ever answer such a request in text, tell him to send '/voice <exact words>'). You CAN make PDFs with /pdf. "
                "Never claim to have checked a website, searched, or verified anything live. For anything that may have changed "
                "(latest versions, news, prices, dates), say plainly that you cannot check live and that your knowledge may be outdated; "
                "give any answer only as 'from memory, may be outdated' and never invent links or release dates.")
        return {"system_instruction": {"parts": [{"text": rule}]}, "contents": [{"role": r, "parts": [{"text": t}]} for r, t in (history or [])] + [{"role": "user", "parts": [{"text": prompt}]}]}

    TTS_MODELS = ("gemini-3.8-flash-tts", "gemini-3.1-flash-tts-preview")

    async def _tts(self, text):
        """Free-tier Gemini TTS -> WAV bytes (None on failure). Output is a WAV file, not an OGG/Opus voice note."""
        import base64, io, wave
        for model in self.TTS_MODELS:
            r = await self.http.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers={"Content-Type": "application/json", "x-goog-api-key": self.gem_key},
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
                    return raw
                rate = 24000
                for part in inl.get("mimeType", "").split(";"):
                    if part.strip().startswith("rate="):
                        rate = int(part.strip()[5:])
                buf = io.BytesIO()
                with wave.open(buf, "wb") as w:
                    w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(raw)
                return buf.getvalue()
            except Exception as ex:
                print("tts parse error", type(ex).__name__)
        return None

    FALLBACK_MODELS = ("gemini-3.7-flash", "gemini-3.1-flash-lite", "gemini-3-flash-preview", "gemini-flash-lite-latest", "gemini-3.8-flash")

    async def _gemini(self, prompt, history=None):
        """Free-tier quotas are per model and small (primary showed limit 20/day): on 429/404 try the next model.
        503/500 are retried once after a short wait. Still free tier only: no billing, no search."""
        models = [self.model] + [m for m in self.FALLBACK_MODELS if m != self.model]
        r = None
        for model in models:
            for attempt in range(2):
                try:
                    r = await self.http.post(
                        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                        headers={"Content-Type": "application/json", "x-goog-api-key": self.gem_key}, json=self._gem(prompt, history), timeout=9)
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
                    return "".join(p.get("text", "") for p in parts).strip() or "..."
                except Exception:
                    return "..."
            if r.status_code not in (429, 404, 500, 503):
                break
        return None

    # ---- router
    async def handle(self, method, path, headers, body):
        headers = {k.lower(): v for k, v in (headers or {}).items()}
        try:
            data = json.loads(body) if body else {}
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        if path == "/health" and method == "GET":
            return 200, {"ok": True, "service": "maya-backend-edge"}
        if path in ("/telegram/bgtest", "/telegram/bgtest-alarm") and hasattr(self.state, "job_set"):
            import hmac as _h
            if not _h.compare_digest(headers.get("x-telegram-bot-api-secret-token", "").encode(), os.environ.get("MAYA_TELEGRAM_SECRET", "x").encode()):
                return self.err(403, "forbidden")
            if method == "POST":
                async def _bg():
                    await asyncio.sleep(5)
                    await self.state.job_set("bgtest-finished", "after 5s background sleep")
                await self.state.job_set("bgtest-started", "")
                if path.endswith("-alarm"):
                    await self.state.job_set("processing", "bgtest-alarm", 5)  # nobody finishes it: alarm must mark it lost, no outbound
                    return 200, {"scheduled": "alarm"}
                if getattr(self, "defer", None):
                    self.defer(_bg())
                return 200, {"scheduled": True}
            return 200, await self.state.job_get()
        if path == "/telegram/webhook" and method == "POST":
            return await self._tg_webhook(headers, data)
        if path.startswith("/v1/"):
            u, tok, e = await self.user(headers)
            if e:
                return e
            return await self._v1(method, path, u, tok, data)
        return self.err(404, "not_found")

    async def _v1(self, method, path, u, tok, data):
        sb, uid = self._sb(tok), u["id"]
        rest = f"{self.url}/rest/v1"
        if path == "/v1/chat" and method == "POST":
            msg = str(data.get("message") or "").strip()
            if not msg:
                return self.err(400, "message_required")
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
        if path == "/v1/memories" and method == "GET":
            r = await self.http.get(f"{rest}/memories", headers=sb, params={
                "select": "id,kind,content,source,created_at,last_used_at", "user_id": f"eq.{uid}", "order": "created_at.desc"})
            return (200, {"memories": r.json()}) if r.status_code < 400 else self.err(500, "internal_error")
        if path == "/v1/memories" and method == "POST":
            kind, content = data.get("kind"), data.get("content")
            if not kind or not content:
                return self.err(400, "kind_and_content_required")
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

    async def _tg_send(self, chat_id, text):
        return await self.http.post(f"https://api.telegram.org/bot{self.tg_token}/sendMessage",
                                    json={"chat_id": chat_id, "text": text[:4000]})

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

    async def _job(self, state, status="", text="", alarm_s=0):
        """Durable job record in the Durable Object: processing -> done | error | timeout (never silent loss)."""
        try:
            if hasattr(self.state, "job_set"):
                await self.state.job_set(state, str(text)[:60], alarm_s)
        except Exception as ex:
            print("job_set error", type(ex).__name__)

    async def _tg_guarded(self, status, text, chat_id):
        try:
            await asyncio.wait_for(self._tg_process(status, text, chat_id), self.BUDGET_S)
            await self._job("done")
        except asyncio.TimeoutError:
            await self._job("timeout")
            print("tg budget exceeded")
            try:
                await self._tg_send(chat_id, "That took too long and I could not finish it. Please send it again in a bit.")
            except Exception:
                pass
        except Exception as ex:
            await self._job("error")
            print("tg guarded error", type(ex).__name__)

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
                    if not vt:
                        await self._tg_send(chat_id, "Usage: /voice <text to speak>")
                    elif len(vt) > 1500:
                        await self._tg_send(chat_id, "Text is too long for one voice clip (max 1500 characters).")
                    else:
                        wav = await self._tts(vt)
                        if not wav:
                            await self._tg_send(chat_id, "Could not generate the audio right now (AI error " + str(getattr(self, "last_gemini_status", "?")) + ").")
                        else:
                            r = await self.http.post_file(f"https://api.telegram.org/bot{self.tg_token}/sendAudio",
                                                          {"chat_id": chat_id, "title": "Maya voice"}, "maya-voice.wav", wav, mime="audio/wav", field="audio")
                            print("tg_sendAudio status", r.status_code)
                            if r.status_code >= 400:
                                r = await self.http.post_file(f"https://api.telegram.org/bot{self.tg_token}/sendDocument",
                                                              {"chat_id": chat_id}, "maya-voice.wav", wav, mime="audio/wav")
                                print("tg_sendDocument(wav) status", r.status_code)
                                if r.status_code >= 400:
                                    await self._tg_send(chat_id, "Audio could not be sent.")
                elif low.startswith("/image") or low.startswith("/video"):
                    await self._tg_send(chat_id, "Image and video generation are not enabled yet.")
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
                        r = await self._tg_send_long(chat_id, reply)
                        print("tg sendMessage status", r.status_code, "chunks", len(self.chunks(reply)))
                        if r.status_code >= 400:
                            await self._tg_send(chat_id, "Reply could not be delivered in full. Please try again.")
                    else:
                        await self._tg_send(chat_id, "Maya could not get an answer from the AI right now (error " + str(getattr(self, "last_gemini_status", "?")) + "). Please try again.")
        except Exception as ex:
            print("telegram handler error", type(ex).__name__, str(ex).replace(self.tg_token, "<token>")[:300])
            try:
                await self._tg_send(chat_id, "Something went wrong while handling that. Please try again.")
            except Exception:
                pass
