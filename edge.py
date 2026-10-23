"""Plain-Python router for the Cloudflare Worker (no FastAPI/pydantic import: they cost ~20 ms CPU per cold start,
over the Free plan's 10 ms limit). Same behaviour as main.py for: /health, /v1/chat, /v1/memories, /v1/tasks, Telegram.
Search is not included (OFF by design). The /auth/reset page is not ported yet (main.py still serves it on Render).
handle() is pure logic over an injected Http and state, so it is testable without Cloudflare."""
import json, os, re

from net import Http
from pdfgen import make_pdf, unsupported_chars, UnsupportedText


def _env(k, d=""):
    return os.environ.get(k, d)


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

    def _gem(self, prompt):
        return {"contents": [{"parts": [{"text": prompt}]}]}

    async def _gemini(self, prompt):
        r = await self.http.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent",
            headers={"Content-Type": "application/json", "x-goog-api-key": self.gem_key}, json=self._gem(prompt))
        if r.status_code != 200:
            return None
        try:
            parts = r.json()["candidates"][0]["content"]["parts"]
            return "".join(p.get("text", "") for p in parts).strip() or "..."
        except Exception:
            return "..."

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
            return 200, {"code": await self.state.pair_start(), "expires_in": 600}
        if path == "/v1/telegram/toggle" and method == "POST":
            if not isinstance(data.get("on"), bool):
                return self.err(400, "on_required")
            await self.state.set_enabled(data["on"])
            return 200, {"enabled": data["on"]}
        if path == "/v1/telegram/revoke" and method == "POST":
            await self.state.revoke()
            return 200, {"revoked": True}
        return self.err(404, "not_found")

    async def _tg_send(self, chat_id, text):
        return await self.http.post(f"https://api.telegram.org/bot{self.tg_token}/sendMessage",
                                    json={"chat_id": chat_id, "text": text[:4000]})

    async def _tg_webhook(self, headers, update):
        status, text, chat_id = await self.state.update(headers, update)
        if not (self.tg_token and isinstance(chat_id, int)):
            return 200, {}
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
                            await self._tg_send(chat_id, "Could not generate the document right now.")
                        else:
                            try:
                                pdf = make_pdf(topic[:80], body)
                            except UnsupportedText:
                                await self._tg_send(chat_id, "The text contains characters the PDF font cannot show (for example Hindi). No PDF was created.")
                                return 200, {}
                            r = await self.http.post_file(f"https://api.telegram.org/bot{self.tg_token}/sendDocument",
                                                          {"chat_id": chat_id}, "maya.pdf", pdf)
                            print("tg_sendDocument status", r.status_code)
                            if r.status_code >= 400:
                                await self._tg_send(chat_id, "PDF could not be sent.")
                elif low.startswith("/image") or low.startswith("/video"):
                    await self._tg_send(chat_id, "Image and video generation are not enabled yet.")
                else:
                    reply = await self._gemini("You are Maya, the owner's personal agent.\nOwner says: " + text)
                    if reply:
                        await self._tg_send(chat_id, reply)
        except Exception as ex:
            print("telegram handler error", type(ex).__name__)
        return 200, {}
