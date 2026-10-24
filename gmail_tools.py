"""Gmail read + draft-first send logic for Maya (pure logic, no network, no tokens).

Read-only scope only: https://www.googleapis.com/auth/gmail.readonly. Sending is never done here: a send command becomes a
Draft that must be confirmed by the owner (explicit recipient + words), and only the confirm step may call the API.
"""
import re

SCOPE_READ = "https://www.googleapis.com/auth/gmail.readonly"
_EMAIL = re.compile(r"^[^@\s<>,;]+@[^@\s<>,;]+\.[A-Za-z]{2,}$")


def parse_command(text: str):
    """'/mail', '/mail unread', '/mail from alice', '/mail search <q>', '/mail send to a@b.com: body' -> dict or None."""
    t = (text or "").strip()
    m = re.match(r"^/mail(?:@\w+)?\s*(.*)$", t, re.I | re.S)
    if not m:
        return None
    rest = m.group(1).strip()
    if not rest or rest.lower() == "unread":
        return {"op": "list", "q": "is:unread in:inbox", "max": 5}
    low = rest.lower()
    if low.startswith("from "):
        who = rest[5:].strip()
        if not re.fullmatch(r"[\w.@+\- ]{1,80}", who):
            return {"op": "error", "why": "bad_sender"}
        return {"op": "list", "q": f'from:"{who}" in:inbox', "max": 5}
    if low.startswith("search "):
        q = rest[7:].strip()[:200]
        if not q:
            return {"op": "error", "why": "empty_query"}
        return {"op": "list", "q": q.replace("\n", " "), "max": 5}
    s = re.match(r"^send\s+to\s+(\S+?)\s*:\s*(.+)$", rest, re.I | re.S)
    if s:
        to, body = s.group(1).strip(), s.group(2).strip()
        if not _EMAIL.match(to):
            return {"op": "error", "why": "bad_recipient"}
        return {"op": "draft", "to": to, "body": body[:5000]}
    return {"op": "error", "why": "unknown"}


def list_url(q: str, n: int = 5) -> str:
    from urllib.parse import quote
    return f"https://gmail.googleapis.com/gmail/v1/users/me/messages?maxResults={int(n)}&q={quote(q)}"


def get_url(mid: str) -> str:
    if not re.fullmatch(r"[0-9a-fA-F]{6,32}", mid or ""):
        raise ValueError("bad id")
    return f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{mid}?format=metadata&metadataHeaders=From&metadataHeaders=Subject&metadataHeaders=Date"


def summarize(msg: dict) -> str:
    h = {x.get("name", "").lower(): x.get("value", "") for x in (msg.get("payload", {}).get("headers") or [])}
    snip = re.sub(r"\s+", " ", msg.get("snippet", "")).strip()[:160]
    return f"{h.get('from','?')[:60]} | {h.get('subject','(no subject)')[:80]} | {h.get('date','')[:31]}\n{snip}"


def render(msgs: list) -> str:
    if not msgs:
        return "Koi matching mail nahi mili."
    return "\n\n".join(f"{i+1}. {summarize(m)}" for i, m in enumerate(msgs))


def draft_preview(cmd: dict) -> str:
    """Shown to the owner. Nothing is sent until the owner replies 'yes send'."""
    return f"Draft (NOT sent)\nTo: {cmd['to']}\n\n{cmd['body']}\n\nBhejne ke liye 'yes send' likho, warna ignore."
