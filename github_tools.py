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
