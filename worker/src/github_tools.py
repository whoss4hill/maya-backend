"""Scoped GitHub tool logic for Maya (pure logic; no network, no tokens).

Guards: only repos in ALLOWED_REPOS; writes only to a non-protected branch (never main/master/default); paths are
sanitised (no '..', no .github/workflows, no secrets-like files); a write is a PLAN the owner must confirm with 'yes push'.
"""
import re

ALLOWED_REPOS = {"whoss4hill/maya-app", "whoss4hill/maya-backend"}
PROTECTED = {"main", "master", "develop", "release"}
_BLOCKED_PATH = re.compile(r"(^|/)(\.github/workflows|\.env[^/]*|.*\.(jks|keystore|pem|p12)|local\.properties)(/|$)", re.I)


def parse_command(text: str):
    """/gh read <repo> <branch> <path>  |  /gh edit <repo> <branch> <path>: <instruction>"""
    m = re.match(r"^/gh(?:@\w+)?\s+(read|edit)\s+(\S+)\s+(\S+)\s+([^\s:]+)\s*(?::\s*(.+))?$", (text or "").strip(), re.S)
    if not m:
        return None
    op, repo, branch, path, instr = m.groups()
    err = check(repo, branch, path, write=(op == "edit"))
    if err:
        return {"op": "error", "why": err}
    if op == "edit" and not instr:
        return {"op": "error", "why": "missing_instruction"}
    return {"op": op, "repo": repo, "branch": branch, "path": path, "instruction": instr}


def check(repo, branch, path, write=False):
    if repo not in ALLOWED_REPOS:
        return "repo_not_allowed"
    if not re.fullmatch(r"[\w./\-]{1,100}", branch or "") or ".." in branch:
        return "bad_branch"
    if write and branch.lower() in PROTECTED:
        return "protected_branch"
    if not path or path.startswith("/") or ".." in path.split("/") or not re.fullmatch(r"[\w./\- ]{1,200}", path):
        return "bad_path"
    if write and _BLOCKED_PATH.search(path):
        return "path_blocked"
    return None


def contents_url(repo, branch, path):
    from urllib.parse import quote
    if check(repo, branch, path):
        raise ValueError("not allowed")
    return f"https://api.github.com/repos/{repo}/contents/{quote(path)}?ref={quote(branch)}"


def plan_preview(cmd, default_branch=None):
    if default_branch and cmd["branch"] == default_branch:
        return "Default branch pe push nahi karungi. Naya/dev branch batao."
    return f"Plan (NOT pushed)\n{cmd['repo']} @ {cmd['branch']}\n{cmd['path']}: {cmd['instruction']}\n\nTests pehle chalenge. Push ke liye 'yes push' likho."


# ---- request builders / response parsers (no network here; the caller sends with the token from a Worker secret) ----
import base64, json as _json

MAX_READ = 20000  # chars returned to chat


def api_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "maya-assistant"}


def parse_read(resp_json: dict) -> str:
    """Decode a contents-API file response. Refuses directories and large files."""
    if not isinstance(resp_json, dict) or resp_json.get("type") != "file":
        return "Not a single file."
    if resp_json.get("size", 0) > 200_000:
        return "File too large to show."
    try:
        txt = base64.b64decode(resp_json.get("content", "")).decode("utf-8")
    except Exception:
        return "File is not text."
    return txt[:MAX_READ] + ("\n...[cut]" if len(txt) > MAX_READ else "")


def write_body(repo, branch, path, new_text, message, prev_sha=None) -> dict:
    """PUT /contents body. Re-checks the guards: never main/protected, only allowed repos/paths."""
    err = check(repo, branch, path, write=True)
    if err:
        raise ValueError(err)
    if len(new_text) > 200_000:
        raise ValueError("too_large")
    body = {"message": message[:200], "content": base64.b64encode(new_text.encode()).decode(), "branch": branch}
    if prev_sha:
        body["sha"] = prev_sha
    return body


def confirm_matches(pending: dict, text: str) -> bool:
    """Only an exact 'yes push' from the bound owner executes a stored plan, and only once."""
    return bool(pending) and (text or "").strip().lower() == "yes push" and not pending.get("used")
