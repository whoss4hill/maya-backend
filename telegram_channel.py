"""Owner-only Telegram channel for Maya (decision logic only; no network, not wired, not live).

Telegram facts used (official Bot API docs, setWebhook):
 - updates arrive as HTTPS POSTs of a JSON Update;
 - if setWebhook is given secret_token (1-256 chars, A-Z a-z 0-9 _ -), every request carries the header
   X-Telegram-Bot-Api-Secret-Token with that value.
Rules enforced here (all default-deny):
 1. channel is OFF unless MAYA_TELEGRAM_ENABLED=1;
 2. the secret header must match MAYA_TELEGRAM_SECRET (constant-time compare);
 3. only a private chat message whose sender id == MAYA_TELEGRAM_OWNER_ID and chat id == sender id is accepted;
    every other sender gets no reply at all (silent drop), so the bot reveals nothing to strangers;
 4. update_id must be higher than the last accepted one (replay/duplicate guard);
 5. at most MAYA_TELEGRAM_RATE messages per 60 s (default 20); text capped at 2000 chars;
 6. text is a command from the owner only through this check; forwarded messages and anything from
    other chats/channels are never treated as commands.
The bot token and secret live in server env only. Messages to the owner use the same chat id.
"""
import hmac, os, time


class State:
    def __init__(self):
        self.last_update_id = 0
        self.times = []


def _cfg():
    return {
        "on": os.environ.get("MAYA_TELEGRAM_ENABLED", "0") == "1",
        "secret": os.environ.get("MAYA_TELEGRAM_SECRET", ""),
        "owner": os.environ.get("MAYA_TELEGRAM_OWNER_ID", ""),
        "rate": int(os.environ.get("MAYA_TELEGRAM_RATE", "20")),
    }


def handle(headers: dict, update: dict, state: State, now: float = None):
    """Returns (status, command_text_or_None). status: ok | disabled | bad_secret | ignored | replay | rate_limited."""
    c = _cfg()
    if not c["on"] or not c["secret"] or not c["owner"].isdigit():
        return "disabled", None
    got = {k.lower(): v for k, v in (headers or {}).items()}.get("x-telegram-bot-api-secret-token", "")
    if not hmac.compare_digest(got.encode(), c["secret"].encode()):
        return "bad_secret", None
    uid = update.get("update_id")
    if not isinstance(uid, int) or uid <= state.last_update_id:
        return "replay", None
    msg = update.get("message")
    if not isinstance(msg, dict):
        return "ignored", None
    frm, chat = msg.get("from") or {}, msg.get("chat") or {}
    text = msg.get("text")
    if (chat.get("type") != "private" or str(frm.get("id")) != c["owner"] or str(chat.get("id")) != c["owner"]
            or frm.get("is_bot") or not isinstance(text, str) or not text.strip()
            or "forward_origin" in msg or "forward_date" in msg):
        return "ignored", None
    now = time.time() if now is None else now
    state.times = [t for t in state.times if now - t < 60]
    if len(state.times) >= c["rate"]:
        return "rate_limited", None
    state.times.append(now)
    state.last_update_id = uid
    return "ok", text.strip()[:2000]


def outbound_chat_id(bound_id):
    """The only chat id Maya may ever send to: the currently bound owner chat (None -> send nothing).
    Callers must use this for every reply, never a chat id taken from an incoming update."""
    return bound_id if isinstance(bound_id, int) and bound_id > 0 else None
