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
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")  # server-side only, NEVER in the APK
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")                        # server-side only, NEVER in the APK
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY or not GEMINI_API_KEY:
    print("Missing env vars: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY and GEMINI_API_KEY are required.", file=sys.stderr)
    sys.exit(1)

SUPABASE_URL = SUPABASE_URL.rstrip("/")

# Service-role key: bypasses RLS, so EVERY query below is
# explicitly scoped with user_id=eq.<caller id>. Never expose
# this key to the app.
SB_HEADERS = {
    "apikey": SUPABASE_SERVICE_ROLE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
}

http = httpx.Client(timeout=30.0)
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


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
    return user, None


def _sb_raise_for_error(res: httpx.Response):
    if res.status_code >= 400:
        raise RuntimeError(f"supabase HTTP {res.status_code}")


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
        json={"contents": [{"role": "user", "parts": [{"text": prompt}]}]},
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
