"""Live /gh and /mail execution paths behind flags + credential secrets.
GitHub: patch frozen at preview, executed only from the exact stored plan, sha re-checked.
Gmail: refresh-token OAuth design; read-only by default; send needs MAYA_GMAIL_SEND_ENABLED.
No real network anywhere: FakeHttp fakes GitHub/Gmail/OAuth."""
import asyncio, json, os, time, base64, pytest
from net import Resp
from state_core import Core

ENV = dict(SUPABASE_URL="https://x.supabase.co", SUPABASE_ANON_KEY="anon", GEMINI_API_KEY="g", MAYA_OWNER_USER_ID="aaa-1",
           MAYA_TELEGRAM_ENABLED="1", MAYA_TELEGRAM_SECRET="s3", TELEGRAM_BOT_TOKEN="tok")

class St:
    def __init__(self): self.core = Core()
    async def update(self, h, u): return self.core.telegram_update(h, u)
    async def pair_start(self, target_id=None): return self.core.pair_start(target_id=target_id)
    async def set_enabled(self, on): self.core.set_enabled(on)
    async def voice_mode_get(self): return self.core.voice_mode
    async def voice_mode_set(self, on): self.core.set_voice_mode(on)
    async def image_mode_get(self): return self.core.image_mode
    async def image_mode_set(self, on): self.core.set_image_mode(on)
    async def fail_add(self, stage, status): self.core.fail_add(stage, status)
    async def revoke(self): self.core.revoke()
    async def model_pref_get(self): return self.core.model_pref
    async def model_pref_set(self, v): self.core.set_model_pref(v)
    async def rl_hit(self, bucket, limit): return self.core.rl_hit(bucket, limit)
    async def bound(self): return self.core.bound_id
    job = {}
    async def job_set(self, s, n="", alarm_s=0): self.job = {"state": s, "note": n, "ts": time.time()}
    async def job_get(self): return self.job
    async def status(self): return {"bound": self.core.bound_id is not None, "enabled": self.core.data["tg"]["owner_enabled"], "bound_suffix": None}

class FakeHttp:
    def __init__(self):
        self.calls = []
        self.gh_sha = "abc123"
    async def _r(self, m, url, **k):
        self.calls.append((m, url, k))
        if url.endswith("/auth/v1/user"): return Resp(200, json.dumps({"id": "aaa-1"}))
        if "api.github.com/repos/" in url and m == "GET":
            return Resp(200, json.dumps({"type": "file", "size": 12, "sha": self.gh_sha,
                                         "content": base64.b64encode(b"old content").decode()}))
        if "api.github.com/repos/" in url and m == "PUT":
            return Resp(201, json.dumps({"content": {"path": "p"}}))
        if url.endswith("oauth2.googleapis.com/token"):
            return Resp(200, json.dumps({"access_token": "acctok", "expires_in": 3600}))
        if "gmail.googleapis.com/gmail/v1/users/me/messages?maxResults" in url:
            return Resp(200, json.dumps({"messages": [{"id": "aa11bb22"}, {"id": "cc33dd44"}]}))
        if "/messages/" in url and "format=metadata" in url:
            return Resp(200, json.dumps({"snippet": "Hello there", "payload": {"headers": [
                {"name": "From", "value": "a@b.co"}, {"name": "Subject", "value": "Hi"}, {"name": "Date", "value": "Mon"}]}}))
        if url.endswith("/messages/send"):
            return Resp(200, json.dumps({"id": "sent1"}))
        if "generateContent" in url:
            return Resp(200, json.dumps({"candidates": [{"content": {"parts": [{"text": "pong"}]}}]}))
        return Resp(200, "[]")
    async def get(self, u, **k): return await self._r("GET", u, **k)
    async def post(self, u, **k): return await self._r("POST", u, **k)
    async def put(self, u, **k): return await self._r("PUT", u, **k)
    async def delete(self, u, **k): return await self._r("DELETE", u, **k)
    async def get_bytes(self, u, **k): return 200, b"B"
    async def post_file(self, url, fields, filename, data, mime="application/pdf", field="document"):
        self.calls.append(("FILE", url, {"fields": fields})); return Resp(200, json.dumps({"ok": True, "result": {"message_id": 1}}))

TOOL_ENVS = ("MAYA_GITHUB_ENABLED", "MAYA_GITHUB_TOKEN", "MAYA_GMAIL_ENABLED", "MAYA_GMAIL_SEND_ENABLED",
             "MAYA_GMAIL_CLIENT_ID", "MAYA_GMAIL_CLIENT_SECRET", "MAYA_GMAIL_REFRESH_TOKEN")

@pytest.fixture
def app(monkeypatch):
    for k, v in ENV.items(): monkeypatch.setenv(k, v)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    for k in TOOL_ENVS:
        monkeypatch.delenv(k, raising=False)
    import importlib, edge; importlib.reload(edge)
    return edge.App(http=FakeHttp(), state=St())

def run(c): return asyncio.run(c)
A = {"Authorization": "Bearer jwt"}
H = {"X-Telegram-Bot-Api-Secret-Token": "s3"}

def pair(app):
    code = run(app.handle("POST", "/v1/telegram/pair/start", A, ""))[1]["code"]
    tg(app, 1, f"/pair {code}")

def tg(app, i, text, uid=42):
    return run(app.handle("POST", "/telegram/webhook", H, json.dumps({"update_id": i, "message":
        {"from": {"id": uid}, "chat": {"id": uid, "type": "private"}, "text": text, "date": int(time.time())}})))

def sent(app):
    return [k["json"]["text"] for _, u, k in app.http.calls if "sendMessage" in u and k.get("json")]

def gmail_on(mp):
    mp.setenv("MAYA_GMAIL_ENABLED", "1"); mp.setenv("MAYA_GMAIL_CLIENT_ID", "cid")
    mp.setenv("MAYA_GMAIL_CLIENT_SECRET", "csec"); mp.setenv("MAYA_GMAIL_REFRESH_TOKEN", "rtok")

def test_gh_read_connected(app, monkeypatch):
    monkeypatch.setenv("MAYA_GITHUB_ENABLED", "1"); monkeypatch.setenv("MAYA_GITHUB_TOKEN", "ghtok")
    pair(app); tg(app, 2, "/gh read whoss4hill/maya-app dev README.md")
    assert "old content" in sent(app)[-1]
    assert all("ghtok" not in t for t in sent(app))

def test_gh_read_not_connected(app):
    pair(app); tg(app, 2, "/gh read whoss4hill/maya-app dev README.md")
    assert "connected nahi" in sent(app)[-1]
    assert not any("api.github.com" in u for _, u, _ in app.http.calls)

def test_gh_edit_preview_freezes_patch_and_one_shot_push(app, monkeypatch):
    monkeypatch.setenv("MAYA_GITHUB_ENABLED", "1"); monkeypatch.setenv("MAYA_GITHUB_TOKEN", "ghtok")
    pair(app)
    tg(app, 2, "/gh edit whoss4hill/maya-app dev a.txt: change it")
    assert "NOT pushed" in sent(app)[-1] and "Badlav" in sent(app)[-1]
    assert not any(m == "PUT" for m, _, _ in app.http.calls)
    n_gen = len([1 for _, u, _ in app.http.calls if "generateContent" in u])
    tg(app, 3, "yes push")
    puts = [k for m, u, k in app.http.calls if m == "PUT"]
    assert len(puts) == 1
    assert base64.b64decode(puts[0]["json"]["content"]) == b"pong" and puts[0]["json"]["sha"] == "abc123"
    # the pushed bytes are the previewed ones: NO new model call happened at confirm time
    assert len([1 for _, u, _ in app.http.calls if "generateContent" in u]) == n_gen
    tg(app, 4, "yes push")  # already consumed
    assert len([1 for m, _, _ in app.http.calls if m == "PUT"]) == 1

def test_gh_edit_rejects_file_changed_since_preview(app, monkeypatch):
    monkeypatch.setenv("MAYA_GITHUB_ENABLED", "1"); monkeypatch.setenv("MAYA_GITHUB_TOKEN", "ghtok")
    pair(app)
    tg(app, 2, "/gh edit whoss4hill/maya-app dev a.txt: change it")
    app.http.gh_sha = "someone-else-changed-it"
    tg(app, 3, "yes push")
    assert "badal gayi" in sent(app)[-1]
    assert not any(m == "PUT" for m, _, _ in app.http.calls)

def test_gh_guards_still_refuse(app, monkeypatch):
    monkeypatch.setenv("MAYA_GITHUB_ENABLED", "1"); monkeypatch.setenv("MAYA_GITHUB_TOKEN", "ghtok")
    pair(app)
    tg(app, 2, "/gh edit whoss4hill/maya-app main a.txt: x")
    assert "protected_branch" in sent(app)[-1]
    assert not any("api.github.com" in u for _, u, _ in app.http.calls)

def test_mail_list_uses_refresh_token_and_caches(app, monkeypatch):
    gmail_on(monkeypatch)
    pair(app)
    tg(app, 2, "/mail"); tg(app, 3, "/mail from alice")
    token_calls = [1 for _, u, _ in app.http.calls if u.endswith("oauth2.googleapis.com/token")]
    assert len(token_calls) == 1  # exchanged once, cached for the second command
    assert "a@b.co" in sent(app)[-1] and "Hi" in sent(app)[-1]
    assert all("rtok" not in t and "acctok" not in t for t in sent(app))

def test_mail_not_connected(app):
    pair(app); tg(app, 2, "/mail")
    assert "connected nahi" in sent(app)[-1]
    assert not any("gmail.googleapis.com" in u for _, u, _ in app.http.calls)

def test_mail_send_never_executes_on_readonly_design(app, monkeypatch):
    gmail_on(monkeypatch)  # read + send flags: send flag deliberately ABSENT
    pair(app)
    tg(app, 2, "/mail send to x@y.com: hello ji")
    assert "NOT sent" in sent(app)[-1] and "read-only" in sent(app)[-1]
    tg(app, 3, "yes send")  # no pending draft was stored, so this must fall through to chat
    assert not any(u.endswith("/messages/send") for _, u, _ in app.http.calls)

def test_mail_send_executes_only_with_send_flag_and_exact_confirm(app, monkeypatch):
    gmail_on(monkeypatch); monkeypatch.setenv("MAYA_GMAIL_SEND_ENABLED", "1")
    pair(app)
    tg(app, 2, "/mail send to x@y.com: hello ji")
    assert "NOT sent" in sent(app)[-1]
    tg(app, 3, "yes push")  # wrong word: no send
    assert not any(u.endswith("/messages/send") for _, u, _ in app.http.calls)
    tg(app, 4, "/mail send to x@y.com: hello ji")
    tg(app, 5, "yes send")
    sends = [k for _, u, k in app.http.calls if u.endswith("/messages/send")]
    assert len(sends) == 1
    raw = base64.urlsafe_b64decode(sends[0]["json"]["raw"]).decode()
    assert "x@y.com" in raw and "hello ji" in raw
