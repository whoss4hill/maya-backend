"""Least-privilege mode: with SUPABASE_ANON_KEY set, Supabase REST calls carry the caller's JWT, not service_role."""
import os, sys, json, asyncio
os.environ.update(SUPABASE_URL="https://x.supabase.co", SUPABASE_ANON_KEY="anon-k", GEMINI_API_KEY="g", MAYA_OWNER_USER_ID="aaa-1")
os.environ.pop("SUPABASE_SERVICE_ROLE_KEY", None)
sys.modules.pop("main", None)
import main  # must import without a service_role key


class Rq:
    headers = {"authorization": "Bearer user-jwt"}
    async def json(self): return {}


def test_starts_without_service_role_and_headers_use_user_jwt():
    calls = []
    async def _get(self, url, **k):
        calls.append((url, k.get("headers")))
        if url.endswith("/auth/v1/user"):
            return main.Resp(200, json.dumps({"id": "aaa-1"}))
        return main.Resp(200, "[]")
    main.http = type("H", (), {"get": _get})()
    out = asyncio.run(main.list_memories(Rq()))
    assert out == {"memories": []}
    auth_h = calls[0][1]; rest_h = calls[1][1]
    assert auth_h == {"apikey": "anon-k", "Authorization": "Bearer user-jwt"}
    assert rest_h == {"apikey": "anon-k", "Authorization": "Bearer user-jwt"}
    assert all("service" not in json.dumps(h) for _, h in calls)
