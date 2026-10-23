import os, sys, json, asyncio, time, subprocess
os.environ.update(SUPABASE_URL="https://x.supabase.co", SUPABASE_ANON_KEY="anon-k", GEMINI_API_KEY="g", MAYA_OWNER_USER_ID="aaa-1",
                  MAYA_TELEGRAM_ENABLED="1", MAYA_TELEGRAM_SECRET="s3", TELEGRAM_BOT_TOKEN="tok")
os.environ.pop("SUPABASE_SERVICE_ROLE_KEY", None)
sys.modules.pop("main", None)
import main
from fastapi.testclient import TestClient
H = {"X-Telegram-Bot-Api-Secret-Token": "s3"}
sent = []

ENV = dict(SUPABASE_URL="https://x.supabase.co", SUPABASE_ANON_KEY="anon-k", GEMINI_API_KEY="g", MAYA_OWNER_USER_ID="aaa-1",
           MAYA_TELEGRAM_ENABLED="1", MAYA_TELEGRAM_SECRET="s3", TELEGRAM_BOT_TOKEN="tok")

def setup():
    os.environ.update(ENV); os.environ.pop("SUPABASE_SERVICE_ROLE_KEY", None)
    main.STATE = main.LocalState(); sent.clear()
    async def _get(self, url, **k): return main.Resp(200, json.dumps({"id": "aaa-1"}))
    async def _post(self, url, **k):
        sent.append((url, k.get("json")))
        if "generateContent" in url:
            return main.Resp(200, json.dumps({"candidates": [{"content": {"parts": [{"text": "pong"}]}}]}))
        return main.Resp(200, "{}")
    main.http = type("H", (), {"get": _get, "post": _post})()
    return TestClient(main.app)

def tg(c, i, uid, text, headers=H, ctype="private"):
    return c.post("/telegram/webhook", headers=headers, json={"update_id": i, "message": {"from": {"id": uid}, "chat": {"id": uid, "type": ctype}, "text": text, "date": int(time.time())}})

def test_owner_routes_need_auth_and_full_flow():
    c = setup()
    assert c.post("/v1/telegram/pair/start").status_code == 401
    A = {"Authorization": "Bearer jwt"}
    code = c.post("/v1/telegram/pair/start", headers=A).json()["code"]
    assert tg(c, 1, 42, f"/pair {code}").status_code == 200 and not sent       # pairing sends nothing
    assert c.get("/v1/telegram/status", headers=A).json() == {"bound": True, "enabled": True, "bound_suffix": "42"}
    tg(c, 2, 42, "ping")
    assert [u for u, _ in sent if "generateContent" in u] and sent[-1][1]["chat_id"] == 42
    n = len(sent); tg(c, 3, 99, "ping"); assert len(sent) == n                  # stranger: nothing sent
    c.post("/v1/telegram/toggle", headers=A, json={"on": False}); tg(c, 4, 42, "ping"); assert len(sent) == n
    c.post("/v1/telegram/toggle", headers=A, json={"on": True}); tg(c, 4, 42, "ping"); assert len(sent) == n  # old update cannot run
    assert c.post("/v1/telegram/toggle", headers=A, json={"on": "yes"}).status_code == 400
    c.post("/v1/telegram/revoke", headers=A); tg(c, 5, 42, "ping"); assert len(sent) == n

def test_bad_secret_silent_200_nothing_sent():
    c = setup(); assert tg(c, 1, 42, "x", headers={"X-Telegram-Bot-Api-Secret-Token": "no"}).status_code == 200 and not sent

def test_jwt_mode_required_fails_closed():
    env = dict(ENV, MAYA_REQUIRE_JWT_MODE="1", PATH=os.environ.get("PATH", ""))
    r = subprocess.run([sys.executable, "-c", "import main"], env=dict(env, SUPABASE_ANON_KEY=""), capture_output=True, cwd=os.path.dirname(__file__) or ".")
    assert r.returncode == 1
    r = subprocess.run([sys.executable, "-c", "import main"], env=dict(env, SUPABASE_SERVICE_ROLE_KEY="svc"), capture_output=True, cwd=os.path.dirname(__file__) or ".")
    assert r.returncode == 1
    r = subprocess.run([sys.executable, "-c", "import main"], env=env, capture_output=True, cwd=os.path.dirname(__file__) or ".")
    assert r.returncode == 0, r.stderr
