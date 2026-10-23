"""Cloudflare Python Worker entry for the Maya backend (STAGING PORT, NOT DEPLOYED, NOT VERIFIED ON THE PLATFORM).

- HTTP API: the existing FastAPI app (main.py) through the ASGI bridge, using net.Http (fetch) instead of httpx.
- Telegram security state: ONE Durable Object (SQLite-backed) so replay guard, rate limit, pairing lockout
  and the On/Off switch are strongly consistent. Logic lives in state_core.py (tested locally).
Unverified here: that the ASGI bridge, os.environ population and the DO RPC calls behave as written on the
platform. Those are checked only by a real staging deploy.
"""
import json, os
from workers import WorkerEntrypoint, DurableObject, Response


class MayaState(DurableObject):
    def __init__(self, ctx, env):
        self.ctx, self.env = ctx, env

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

    async def status(self):
        core = await self._core()
        b = core.bound_id
        return json.dumps({"bound": b is not None, "enabled": core.data["tg"]["owner_enabled"],
                           "bound_suffix": str(b)[-4:] if b else None})


def _load_env(env):
    for k in dir(env):
        v = getattr(env, k, None)
        if isinstance(v, str) and k.isupper():
            os.environ.setdefault(k, v)


class DOState:
    """Adapter with the same async interface as main.LocalState, backed by the single Durable Object."""
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


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        _load_env(self.env)
        import asgi
        import main
        from main import app
        main.STATE = DOState(self.env.MAYA_STATE.get(self.env.MAYA_STATE.idFromName("maya")))
        return await asgi.fetch(app, request.js_object, self.env)
