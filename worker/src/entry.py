"""Cloudflare Python Worker entry (STAGING, plain router: no FastAPI import, to stay under the Free 10 ms CPU limit).
Telegram security state lives in ONE Durable Object (SQLite-backed, strongly consistent): state_core.Core."""
import json, os
from workers import WorkerEntrypoint, DurableObject, Response


NAMES = ("SUPABASE_URL", "SUPABASE_ANON_KEY", "GEMINI_API_KEY", "GEMINI_MODEL", "MAYA_OWNER_USER_ID",
         "MAYA_TELEGRAM_SECRET", "TELEGRAM_BOT_TOKEN", "MAYA_TELEGRAM_ENABLED", "MAYA_REQUIRE_JWT_MODE")


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

    async def pair_start(self):
        core = await self._core(); code = core.pair_start(); await self._save(core); return code

    async def set_enabled(self, on):
        core = await self._core(); core.set_enabled(bool(on)); await self._save(core)

    async def revoke(self):
        core = await self._core(); core.revoke(); await self._save(core)

    async def bound(self):
        core = await self._core(); return core.bound_id

    async def history(self):
        raw = await self.ctx.storage.get("hist")
        return raw if raw else "[]"

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

    async def pair_start(self):
        return await self.stub.pair_start()

    async def set_enabled(self, on):
        await self.stub.set_enabled(on)

    async def revoke(self):
        await self.stub.revoke()

    async def status(self):
        return json.loads(await self.stub.status())

    async def history(self):
        return json.loads(await self.stub.history())

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


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        _load_env(self.env)
        from urllib.parse import urlparse
        from edge import App
        from net import Http
        path = urlparse(request.url).path
        if path == "/health":
            return Response(json.dumps({"ok": True, "service": "maya-backend-edge"}), headers={"content-type": "application/json"})
        app = App(http=Http(), state=DOState(self.env.MAYA_STATE.get(self.env.MAYA_STATE.idFromName("maya"))))
        body = await request.text() if request.method in ("POST", "PUT", "PATCH") else ""
        status, payload = await app.handle(request.method, path, _hdrs(request), body)
        if payload is None:
            return Response("", status=status)
        return Response(json.dumps(payload), status=status, headers={"content-type": "application/json"})
