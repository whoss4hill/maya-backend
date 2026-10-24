"""Cloudflare Python Worker entry (STAGING, plain router: no FastAPI import, to stay under the Free 10 ms CPU limit).
Telegram security state lives in ONE Durable Object (SQLite-backed, strongly consistent): state_core.Core."""
import json, os
from workers import WorkerEntrypoint, DurableObject, Response
import time, uuid


NAMES = ("SUPABASE_URL", "SUPABASE_ANON_KEY", "GEMINI_API_KEY", "GEMINI_MODEL", "MAYA_OWNER_USER_ID",
         "MAYA_TELEGRAM_SECRET", "TELEGRAM_BOT_TOKEN", "MAYA_TELEGRAM_ENABLED", "MAYA_REQUIRE_JWT_MODE",
         "OPENROUTER_API_KEY", "GROQ_API_KEY",
         "MAYA_RL_CHAT_PER_MIN", "MAYA_RL_IMAGE_PER_MIN", "MAYA_RL_WRITE_PER_MIN", "MAYA_RL_READ_PER_MIN")


def _load_env(env):
    for k in NAMES:
        try:
            v = getattr(env, k)
        except Exception:
            continue
        if v is not None and not isinstance(v, bool):
            os.environ[k] = str(v)


class MayaState(DurableObject):
    def __init__(self, ctx, env):
        self.ctx, self.env = ctx, env
        _load_env(env)

    async def _core(self):
        from state_core import Core
        raw = await self.ctx.storage.get("core")
        return Core(json.loads(raw) if raw else None)

    async def _save(self, core):
        await self.ctx.storage.put("core", json.dumps(core.data))

    async def update(self, headers_json, update_json):
        core = await self._core()
        out = core.telegram_update(json.loads(headers_json), json.loads(update_json))
        await self._save(core)
        return json.dumps(out)

    async def pair_start(self, target_id=None):
        core = await self._core(); code = core.pair_start(target_id=target_id); await self._save(core); return code

    async def rl_hit(self, bucket, limit):
        core = await self._core()
        out = core.rl_hit(str(bucket), int(limit))
        await self._save(core)
        return json.dumps(out)

    async def set_enabled(self, on):
        core = await self._core(); core.set_enabled(bool(on)); await self._save(core)

    async def model_pref_get(self):
        core = await self._core(); return core.model_pref

    async def model_pref_set(self, v):
        core = await self._core(); core.set_model_pref(v); await self._save(core)

    async def revoke(self):
        core = await self._core(); core.revoke(); await self._save(core)

    async def bound(self):
        core = await self._core(); return core.bound_id

    async def history(self):
        raw = await self.ctx.storage.get("hist")
        return raw if raw else "[]"

    async def job_set(self, state, note="", alarm_s=0):
        import time as _t
        await self.ctx.storage.put("job", json.dumps({"state": state, "note": note, "ts": _t.time()}))
        if alarm_s:  # watchdog: if the job is still "processing" when this fires, tell the owner instead of losing it silently
            await self.ctx.storage.setAlarm(int((_t.time() + alarm_s) * 1000))

    async def alarm(self):
        raw = await self.ctx.storage.get("job")
        job = json.loads(raw) if raw else {}
        if job.get("state") != "processing":
            return
        job["state"] = "lost"
        await self.ctx.storage.put("job", json.dumps(job))
        core = await self._core(); b = core.bound_id
        if b:
            from net import Http
            _load_env(self.env)
            await Http().post(f"https://api.telegram.org/bot{os.environ.get('TELEGRAM_BOT_TOKEN','')}/sendMessage",
                              json={"chat_id": b, "text": "I could not finish your last message. Please send it again."})

    async def job_get(self):
        raw = await self.ctx.storage.get("job")
        return raw if raw else "{}"

    # ---- phone socket: one authenticated owner socket, event push (no polling), no offline queue ----
    async def fetch(self, request):
        from js import WebSocketPair
        for old in self.ctx.getWebSockets():  # single bound owner device: newest connection wins
            try:
                old.close(1000, "replaced")
            except Exception:
                pass
        pair = WebSocketPair.new()
        client, server = pair.object_values()
        self.ctx.acceptWebSocket(server)
        return Response(None, status=101, web_socket=client)

    async def push_command(self, cmd_json):
        socks = list(self.ctx.getWebSockets())
        if not socks:
            return "offline"
        cmd = json.loads(cmd_json)
        cmd["exp"] = int(time.time()) + 60
        pend = json.loads(await self.ctx.storage.get("pend") or "{}")
        pend = {k: v for k, v in pend.items() if v > time.time()}
        pend[cmd["id"]] = cmd["exp"]
        await self.ctx.storage.put("pend", json.dumps(pend))
        socks[0].send(json.dumps(cmd))
        return "sent"

    async def webSocketMessage(self, ws, message):
        try:
            m = json.loads(message if isinstance(message, str) else bytes(message).decode())
        except Exception:
            return
        if not isinstance(m, dict) or "ack" not in m:
            return
        pend = json.loads(await self.ctx.storage.get("pend") or "{}")
        if m["ack"] not in pend:  # unknown/expired/replayed command id: ignore
            return
        del pend[m["ack"]]
        await self.ctx.storage.put("pend", json.dumps(pend))
        core = await self._core(); b = core.bound_id
        if b:
            from net import Http
            _load_env(self.env)
            txt = "Phone: " + str(m.get("text", m.get("status", "ok")))[:500]
            await Http().post(f"https://api.telegram.org/bot{os.environ.get('TELEGRAM_BOT_TOKEN','')}/sendMessage", json={"chat_id": b, "text": txt})

    async def webSocketClose(self, ws, code, reason, was_clean):
        try:
            ws.close(code, reason)
        except Exception:
            pass

    async def add_turn(self, user_text, reply_text):
        raw = await self.ctx.storage.get("hist")
        h = json.loads(raw) if raw else []
        h.append(["user", str(user_text)[:1500]]); h.append(["model", str(reply_text)[:1500]])
        await self.ctx.storage.put("hist", json.dumps(h[-12:]))

    async def status(self):
        core = await self._core(); b = core.bound_id
        return json.dumps({"bound": b is not None, "enabled": core.data["tg"]["owner_enabled"],
                           "bound_suffix": str(b)[-4:] if b else None,
                           "bound_sha256_12": __import__("hashlib").sha256(str(b).encode()).hexdigest()[:12] if b else None})


class DOState:
    def __init__(self, stub):
        self.stub = stub

    async def update(self, headers, update):
        return tuple(json.loads(await self.stub.update(json.dumps(headers), json.dumps(update))))

    async def pair_start(self, target_id=None):
        return await self.stub.pair_start(target_id)

    async def rl_hit(self, bucket, limit):
        return tuple(json.loads(await self.stub.rl_hit(bucket, limit)))

    async def set_enabled(self, on):
        await self.stub.set_enabled(on)

    async def model_pref_get(self):
        return await self.stub.model_pref_get()

    async def model_pref_set(self, v):
        await self.stub.model_pref_set(v)

    async def revoke(self):
        await self.stub.revoke()

    async def status(self):
        return json.loads(await self.stub.status())

    async def history(self):
        return json.loads(await self.stub.history())

    async def job_set(self, state, note="", alarm_s=0):
        await self.stub.job_set(state, note, alarm_s)

    async def job_get(self):
        return json.loads(await self.stub.job_get())

    async def push_command(self, cmd):
        return await self.stub.push_command(json.dumps(cmd))

    async def add_turn(self, u, m):
        await self.stub.add_turn(u, m)

    async def bound(self):
        b = await self.stub.bound()
        return int(b) if b is not None else None


def _hdrs(request):
    h = {}
    for k in ("authorization", "x-telegram-bot-api-secret-token", "content-type"):
        v = request.headers.get(k)
        if v:
            h[k] = str(v)
    return h



class _AI:
    """Cloudflare Workers AI binding (Free plan: 10,000 Neurons/day, then the platform refuses; no paid plan here)."""
    def __init__(self, env):
        self.env = env

    async def image(self, prompt, model=None):
        # flux-1-schnell stays the default. FLUX.2 models (e.g. flux-2-klein-4b,
        # runtime-verified 2026-10-24) require multipart input: the binding takes
        # multipart={body, contentType} from a serialized FormData, NOT plain fields.
        try:
            from pyodide.ffi import to_js
            from js import Object
            from state_core import DEFAULT_IMAGE_MODEL
            m = model or DEFAULT_IMAGE_MODEL
            if m.startswith("@cf/black-forest-labs/flux-2"):
                from js import FormData, Response as _JsResponse
                form = FormData.new()
                form.append("prompt", prompt)
                form.append("width", "1024")
                form.append("height", "1024")
                fr = _JsResponse.new(form)
                payload = to_js({"multipart": {"body": fr.body,
                                               "contentType": fr.headers.get("content-type")}},
                                dict_converter=Object.fromEntries)
                r = await self.env.AI.run(m, payload)
            else:
                r = await self.env.AI.run(m, to_js({"prompt": prompt, "steps": 4},
                                                   dict_converter=Object.fromEntries))
            img = getattr(r, "image", None)
            return str(img) if img else None
        except Exception as ex:
            print("ai image error", type(ex).__name__)
            return None

    async def text(self, prompt, model=None):
        try:
            from pyodide.ffi import to_js
            from js import Object
            from state_core import DEFAULT_WORKER_MODEL
            r = await self.env.AI.run(model or DEFAULT_WORKER_MODEL, to_js({
                "messages": [{"role": "system", "content": "You are Maya, a concise personal assistant. You are female: use feminine Hindi verb forms for yourself (karti hoon, sakti hoon). Reply in simple Hinglish unless asked otherwise. Never claim to have done real-world actions or looked things up live."},
                             {"role": "user", "content": prompt}], "max_tokens": 600}, dict_converter=Object.fromEntries))
            out = getattr(r, "response", None)
            if out is None:
                # reasoning/chat models (gpt-oss, qwen3.8, gemma-4) return an
                # OpenAI chat-completion shape instead of {"response": ...}
                ch = getattr(r, "choices", None)
                if ch:
                    msg = getattr(ch[0], "message", None)
                    out = getattr(msg, "content", None) if msg is not None else None
            out = str(out).strip() if out else None
            return out or None
        except Exception as ex:
            print("ai text error", type(ex).__name__)
            return None


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        _load_env(self.env)
        from urllib.parse import urlparse
        from edge import App
        from net import Http
        path = urlparse(request.url).path
        if path == "/health":
            return Response(json.dumps({"ok": True, "service": "maya-backend-edge"}), headers={"content-type": "application/json"})
        stub = self.env.MAYA_STATE.get(self.env.MAYA_STATE.idFromName("maya"))
        app = App(http=Http(), state=DOState(stub))
        app.ai = _AI(self.env)
        import asyncio as _a
        app.defer = lambda coro: self.ctx.waitUntil(_a.ensure_future(coro))
        if path == "/v1/phone/ws":
            if str(request.headers.get("upgrade") or "").lower() != "websocket":
                return Response("expected websocket", status=426)
            u, _t, err = await app.user(_hdrs(request))
            if err:
                return Response(json.dumps(err[1]), status=err[0], headers={"content-type": "application/json"})
            return await stub.fetch(request)
        body = await request.text() if request.method in ("POST", "PUT", "PATCH") else ""
        status, payload = await app.handle(request.method, path, _hdrs(request), body)
        if payload is None:
            return Response("", status=status)
        return Response(json.dumps(payload), status=status, headers={"content-type": "application/json"})
