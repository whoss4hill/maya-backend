# =============================================================
# MAYA backend API (section 10 of the spec) - Python/FastAPI port
# Small FastAPI service for Render's free tier.
# The Android app never calls Gemini or the database directly -
# it calls this API. The Gemini key and the Supabase
# service-role key live here only, as environment variables.
#
# HTTP contract is identical to the previous Node/Express
# version: same paths, same request/response JSON.
# =============================================================
import os
import sys
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")  # server-side only, NEVER in the APK
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")                        # server-side only, NEVER in the APK
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY or not GEMINI_API_KEY:
    print("Missing env vars: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY and GEMINI_API_KEY are required.", file=sys.stderr)
    sys.exit(1)

SUPABASE_URL = SUPABASE_URL.rstrip("/")

# Single-owner enforcement. MAYA_OWNER_USER_ID is the immutable Supabase auth
# user id (uuid) of the one permitted user. It is NOT the email (emails can be
# changed/unverified). Unset or empty => fail closed: every request is denied.
MAYA_OWNER_USER_ID = os.environ.get("MAYA_OWNER_USER_ID", "").strip().lower()

# Public (anon) key, used only by the password-reset page. It is public by design
# (it ships in any client); never put the service-role key here.
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "").strip()


def is_owner(user: dict) -> bool:
    if not MAYA_OWNER_USER_ID:
        return False
    return str(user.get("id", "")).strip().lower() == MAYA_OWNER_USER_ID

# Service-role key: bypasses RLS, so EVERY query below is
# explicitly scoped with user_id=eq.<caller id>. Never expose
# this key to the app.
SB_HEADERS = {
    "apikey": SUPABASE_SERVICE_ROLE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
}

http = httpx.Client(timeout=30.0)
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


# -------------------------------------------------------------
# Password reset landing page (Supabase recovery email redirects here).
# The recovery token arrives in the URL fragment (#access_token=...&type=recovery),
# which browsers never send to the server. The page removes it from the address
# bar at once, then calls Supabase PUT /auth/v1/user with that token and the
# PUBLIC anon key. No service-role key, no token logging, no server-side state.
# -------------------------------------------------------------
_RESET_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer"><title>Maya - set a new password</title>
<style>body{font-family:system-ui,sans-serif;max-width:380px;margin:12vh auto;padding:0 16px}
input,button{width:100%;padding:12px;margin:8px 0;font-size:16px;box-sizing:border-box}
#m{min-height:1.4em}</style></head><body>
<h2>Set a new password</h2>
<form id="f" hidden><input id="p" type="password" minlength="8" autocomplete="new-password" placeholder="New password (8+ characters)" required>
<input id="c" type="password" minlength="8" autocomplete="new-password" placeholder="Confirm password" required>
<button id="b" type="submit">Save password</button></form><p id="m"></p>
<script>
(function(){
var U="__SUPABASE_URL__",K="__SUPABASE_ANON_KEY__";
var h=new URLSearchParams(location.hash.replace(/^#/,""));
var t=h.get("access_token"),ty=h.get("type"),e=h.get("error_description");
history.replaceState(null,"",location.pathname);
var m=document.getElementById("m"),f=document.getElementById("f");
if(e){m.textContent="This link is not valid or has expired. Request a new reset email.";return;}
if(!t||ty!=="recovery"){m.textContent="Open this page from the reset email link.";return;}
f.hidden=false;
f.addEventListener("submit",function(ev){ev.preventDefault();
var p=document.getElementById("p").value,c=document.getElementById("c").value;
if(p!==c){m.textContent="Passwords do not match.";return;}
if(p.length<8){m.textContent="Use at least 8 characters.";return;}
document.getElementById("b").disabled=true;m.textContent="Saving...";
fetch(U+"/auth/v1/user",{method:"PUT",headers:{"apikey":K,"Authorization":"Bearer "+t,"Content-Type":"application/json"},body:JSON.stringify({password:p})})
.then(function(r){if(r.ok){f.hidden=true;m.textContent="Password updated. You can sign in to Maya now.";}
else{document.getElementById("b").disabled=false;m.textContent="Could not update the password. The link may have expired; request a new reset email.";}})
.catch(function(){document.getElementById("b").disabled=false;m.textContent="Network error. Try again.";});
});})();
</script></body></html>"""


@app.get("/auth/reset", response_class=HTMLResponse, include_in_schema=False)
def auth_reset():
    if not SUPABASE_ANON_KEY:
        return HTMLResponse("Password reset page is not configured.", status_code=503,
                            headers={"Cache-Control": "no-store"})
    body = _RESET_HTML.replace("__SUPABASE_URL__", SUPABASE_URL).replace("__SUPABASE_ANON_KEY__", SUPABASE_ANON_KEY)
    return HTMLResponse(body, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                                       "X-Content-Type-Options": "nosniff"})


def _err(status: int, code: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": code})


# -------------------------------------------------------------
# 404 + error handling.
# Logging rule from the spec: enough detail to debug, but NEVER
# keys, tokens, or user message bodies - so we log only the
# method, path, status and the error message.
# -------------------------------------------------------------
@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    if exc.status_code == 404:
        return _err(404, "not_found")
    if exc.status_code == 405:
        return _err(404, "not_found")
    return _err(exc.status_code, "internal_error")


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return _err(400, "bad_request")


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    print(f"{request.method} {request.url.path} -> 500: {exc}", file=sys.stderr)
    return _err(500, "internal_error")


# -------------------------------------------------------------
# Health check - Render + uptime monitors ping this.
# -------------------------------------------------------------
@app.get("/health")
async def health():
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return {"ok": True, "service": "maya-backend", "time": now}


# -------------------------------------------------------------
# Auth: the app sends the user's Supabase sign-in token as
# "Authorization: Bearer <token>". We verify it and get the
# user. The anon key never reaches this server.
# -------------------------------------------------------------
async def require_user(request: Request):
    header = request.headers.get("authorization", "")
    token = header[7:] if header.startswith("Bearer ") else None
    if not token:
        return None, _err(401, "missing_token")
    try:
        res = http.get(
            f"{SUPABASE_URL}/auth/v1/user",
            headers={"apikey": SUPABASE_SERVICE_ROLE_KEY, "Authorization": f"Bearer {token}"},
        )
    except Exception as e:
        raise e
    if res.status_code != 200:
        return None, _err(401, "invalid_token")
    user = res.json()
    if not user or not user.get("id"):
        return None, _err(401, "invalid_token")
    # The token was verified by Supabase Auth above (signature, expiry, project).
    # Now require the verified identity to be the configured owner.
    if not is_owner(user):
        return None, _err(403, "not_authorized")
    return user, None


def _sb_raise_for_error(res: httpx.Response):
    if res.status_code >= 400:
        raise RuntimeError(f"supabase HTTP {res.status_code}")



# -------------------------------------------------------------
# Live web search (Gemini "Grounding with Google Search").
# Docs: https://ai.google.dev/gemini-api/docs/generate-content/google-search
# Billing note (same page, Pricing): Gemini 3+ bills per search query the model runs;
# 5,000 free queries/month shared across the project, then paid. To avoid ever reaching paid usage
# this server keeps a conservative monthly counter and turns the tool off at MAYA_SEARCH_MONTHLY_CAP.
# The counter is in-process (resets on restart), so the cap is a guard, not an accounting system.
# MAYA_WEB_SEARCH=0 turns the feature off.
# -------------------------------------------------------------
import time as _time

WEB_SEARCH_ON = os.environ.get("MAYA_WEB_SEARCH", "1") != "0"
SEARCH_MONTHLY_CAP = int(os.environ.get("MAYA_SEARCH_MONTHLY_CAP", "2000"))
_search_used = {"month": "", "n": 0}


def _gemini_payload(prompt: str) -> dict:
    payload = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
    if _search_allowed():
        payload["tools"] = [{"google_search": {}}]
    return payload


def _search_month() -> str:
    return _time.strftime("%Y-%m", _time.gmtime())


def _search_allowed() -> bool:
    if not WEB_SEARCH_ON:
        return False
    if _search_used["month"] != _search_month():
        _search_used.update(month=_search_month(), n=0)
    return _search_used["n"] < SEARCH_MONTHLY_CAP


def _search_record(data: dict) -> list:
    """Counts the queries the model actually ran and returns [(title, uri)] sources (max 5, de-duplicated)."""
    try:
        gm = data["candidates"][0].get("groundingMetadata") or {}
    except Exception:
        return []
    queries = gm.get("webSearchQueries") or []
    if queries:
        if _search_used["month"] != _search_month():
            _search_used.update(month=_search_month(), n=0)
        _search_used["n"] += len([q for q in queries if str(q).strip()])
    out, seen = [], set()
    for ch in gm.get("groundingChunks") or []:
        web = (ch or {}).get("web") or {}
        uri = web.get("uri")
        if uri and uri not in seen:
            seen.add(uri)
            out.append((web.get("title") or uri, uri))
        if len(out) >= 5:
            break
    return out


def _with_sources(reply: str, sources: list) -> str:
    if not sources:
        return reply
    return reply + "\n\nSources:\n" + "\n".join(f"- {t}: {u}" for t, u in sources)


# -------------------------------------------------------------
# POST /v1/chat - the main thinking loop.
# Body: { message: string }
# 1. Recall this user's recent memories.
# 2. Ask Gemini (key stays here).
# 3. Save both lines to this user's conversation log.
# -------------------------------------------------------------
@app.post("/v1/chat")
async def chat(request: Request):
    user, err = await require_user(request)
    if err:
        return err

    try:
        body = await request.json()
    except Exception:
        body = {}
    message = str((body or {}).get("message") or "").strip()
    if not message:
        return _err(400, "message_required")

    res = http.get(
        f"{SUPABASE_URL}/rest/v1/memories",
        headers=SB_HEADERS,
        params={
            "select": "kind,content",
            "user_id": f"eq.{user['id']}",
            "order": "last_used_at.desc.nullslast",
            "limit": "20",
        },
    )
    memories = res.json() if res.status_code == 200 else []

    memory_block = "\n".join(f"- ({m.get('kind')}) {m.get('content')}" for m in memories)

    prompt = (
        "You are Maya, the owner's personal agent.\n"
        + (f"What you remember about the owner:\n{memory_block}\n\n" if memory_block else "")
        + f"Owner says: {message}"
    )

    gemini_res = http.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
        headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY},
        json=_gemini_payload(prompt),
    )
    if gemini_res.status_code != 200:
        # Status only - never log the response body (may echo the prompt).
        print(f"Gemini call failed: HTTP {gemini_res.status_code}", file=sys.stderr)
        return _err(502, "gemini_unavailable")
    gemini_data = gemini_res.json()
    try:
        parts = gemini_data["candidates"][0]["content"]["parts"]
        reply = "".join(p.get("text", "") for p in parts).strip() or "..."
    except Exception:
        reply = "..."
    reply = _with_sources(reply, _search_record(gemini_data))

    ins = http.post(
        f"{SUPABASE_URL}/rest/v1/conversations",
        headers={**SB_HEADERS, "Content-Type": "application/json"},
        json=[
            {"user_id": user["id"], "role": "user", "text": message},
            {"user_id": user["id"], "role": "maya", "text": reply},
        ],
    )
    _sb_raise_for_error(ins)

    return {"reply": reply}


# -------------------------------------------------------------
# Memories - recall, save, forget (all scoped to the caller).
# -------------------------------------------------------------
@app.get("/v1/memories")
async def list_memories(request: Request):
    user, err = await require_user(request)
    if err:
        return err
    res = http.get(
        f"{SUPABASE_URL}/rest/v1/memories",
        headers=SB_HEADERS,
        params={
            "select": "id,kind,content,source,created_at,last_used_at",
            "user_id": f"eq.{user['id']}",
            "order": "created_at.desc",
        },
    )
    _sb_raise_for_error(res)
    return {"memories": res.json()}


@app.post("/v1/memories", status_code=201)
async def create_memory(request: Request):
    user, err = await require_user(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body or {}
    kind, content, source = body.get("kind"), body.get("content"), body.get("source")
    if not kind or not content:
        return _err(400, "kind_and_content_required")
    res = http.post(
        f"{SUPABASE_URL}/rest/v1/memories",
        headers={**SB_HEADERS, "Content-Type": "application/json", "Prefer": "return=representation"},
        params={"select": "id"},
        json={"user_id": user["id"], "kind": kind, "content": content, "source": source or "chat"},
    )
    _sb_raise_for_error(res)
    return {"id": res.json()[0]["id"]}


@app.delete("/v1/memories/{memory_id}", status_code=204)
async def delete_memory(memory_id: str, request: Request):
    user, err = await require_user(request)
    if err:
        return err
    # "Forget completely": hard delete, no archive.
    res = http.delete(
        f"{SUPABASE_URL}/rest/v1/memories",
        headers=SB_HEADERS,
        params={"id": f"eq.{memory_id}", "user_id": f"eq.{user['id']}"},
    )
    _sb_raise_for_error(res)
    return JSONResponse(status_code=204, content=None)


# -------------------------------------------------------------
# Tasks - ongoing work, scoped to the caller.
# -------------------------------------------------------------
@app.get("/v1/tasks")
async def list_tasks(request: Request):
    user, err = await require_user(request)
    if err:
        return err
    res = http.get(
        f"{SUPABASE_URL}/rest/v1/tasks",
        headers=SB_HEADERS,
        params={
            "select": "id,title,status,due,notes,created_at,updated_at",
            "user_id": f"eq.{user['id']}",
            "order": "created_at.desc",
        },
    )
    _sb_raise_for_error(res)
    return {"tasks": res.json()}


@app.post("/v1/tasks", status_code=201)
async def create_task(request: Request):
    user, err = await require_user(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body or {}
    title, due, notes = body.get("title"), body.get("due"), body.get("notes")
    if not title:
        return _err(400, "title_required")
    res = http.post(
        f"{SUPABASE_URL}/rest/v1/tasks",
        headers={**SB_HEADERS, "Content-Type": "application/json", "Prefer": "return=representation"},
        params={"select": "id"},
        json={"user_id": user["id"], "title": title, "due": due or None, "notes": notes or None},
    )
    _sb_raise_for_error(res)
    return {"id": res.json()[0]["id"]}


# Catch-all 404 for unmatched paths (matches the Node version).
@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def catch_all(path: str):
    return _err(404, "not_found")
