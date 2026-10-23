import os, importlib, asyncio, sys
os.environ.update(SUPABASE_URL="https://x.supabase.co", SUPABASE_SERVICE_ROLE_KEY="k", GEMINI_API_KEY="g")
import httpx

class FakeReq:
    def __init__(self, tok): self.headers = {"authorization": f"Bearer {tok}"} if tok else {}

def load(owner):
    os.environ["MAYA_OWNER_USER_ID"] = owner
    sys.modules.pop("main", None)
    import main
    return main

def run(main, status, body):
    async def _get(self, *a, **k): return main.Resp(status, __import__("json").dumps(body))
    main.http = type("H", (), {"get": _get})()
    return asyncio.run(main.require_user(FakeReq("t")))

def test_owner_allowed():
    m = load("AAA-1"); u, e = run(m, 200, {"id": "aaa-1"}); assert e is None and u["id"] == "aaa-1"

def test_other_user_denied():
    m = load("aaa-1"); u, e = run(m, 200, {"id": "bbb-2"}); assert u is None and e.status_code == 403

def test_unset_owner_fails_closed():
    m = load(""); u, e = run(m, 200, {"id": "aaa-1"}); assert u is None and e.status_code == 403

def test_invalid_token_401():
    m = load("aaa-1"); u, e = run(m, 401, {}); assert e.status_code == 401

def test_missing_token_401():
    m = load("aaa-1"); u, e = asyncio.run(m.require_user(FakeReq(None))); assert e.status_code == 401
