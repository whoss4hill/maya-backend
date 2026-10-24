"""Single source of truth for Maya's Telegram security state, as plain JSON-able data.

Runs inside ONE Cloudflare Durable Object (single-threaded, strongly consistent), so the replay guard, rate limit,
pairing lockout and On/Off switch cannot race across Worker isolates. Pure logic: no network, no Cloudflare imports.
The caller persists `core.data` after every call.
"""
import hmac, os, time
from telegram_channel import State, handle
from telegram_pairing import Pairing


class Core:
    def __init__(self, data: dict = None):
        self.data = data if data is not None else {}
        d = self.data
        d.setdefault("tg", {"last_update_id": 0, "times": [], "owner_enabled": True})
        d.setdefault("pair", {})
        d.setdefault("audit", [])

    # -- helpers
    def _audit(self, e):
        self.data["audit"] = (self.data["audit"] + [e])[-50:]

    def _pairing(self):
        return Pairing(self.data["pair"], audit=self._audit)

    def _state(self):
        t = self.data["tg"]
        st = State()
        st.last_update_id, st.times, st.owner_enabled = t["last_update_id"], list(t["times"]), t["owner_enabled"]
        return st

    def _save_state(self, st):
        self.data["tg"] = {"last_update_id": st.last_update_id, "times": st.times, "owner_enabled": st.owner_enabled}

    @property
    def bound_id(self):
        return self.data["pair"].get("bound_id")

    # -- owner actions (caller must have authenticated the Maya owner)
    MODEL_PREFS = ("auto", "backup_only")

    @property
    def model_pref(self):
        v = self.data.get("model_pref", "auto")
        return v if v in self.MODEL_PREFS else "auto"

    def set_model_pref(self, v):
        if v not in self.MODEL_PREFS:
            raise ValueError("bad_model_pref")
        self.data["model_pref"] = v

    def set_enabled(self, on: bool):
        self.data["tg"]["owner_enabled"] = bool(on)
        self._audit({"event": "toggle", "on": bool(on)})

    def pair_start(self, now=None, target_id=None):
        return self._pairing().start(now, target_id)

    def revoke(self):
        self._pairing().revoke()

    # -- Telegram webhook
    def telegram_update(self, headers: dict, update: dict, now: float = None):
        """Returns (status, command_or_None, reply_chat_id_or_None). reply_chat_id is only ever the bound id."""
        now = time.time() if now is None else now
        bound = self.bound_id
        st = self._state()
        txt = ((update.get("message") or {}).get("text") or "") if isinstance(update, dict) else ""
        rebind = bound is not None and "pending" in self.data["pair"] and str(txt).lstrip().lower().startswith("/pair")
        if bound is None or rebind:
            status = self._unbound(headers, update, st, now)
            self._save_state(st)
            # a pairing message never carries a command; only a successful pairing gets a fixed ack to the new bound chat
            return status, None, (self.bound_id if status == "paired" else None)
        status, text = handle(headers, update, st, now=now, owner=str(bound))
        self._save_state(st)
        return status, text, (bound if status == "ok" else None)

    def _unbound(self, headers, update, st, now):
        """Nothing is paired: the only thing that can happen is a valid `/pair CODE` from a private chat."""
        secret = os.environ.get("MAYA_TELEGRAM_SECRET", "")
        if os.environ.get("MAYA_TELEGRAM_ENABLED", "0") != "1" or not secret:
            return "disabled"
        got = {k.lower(): v for k, v in (headers or {}).items()}.get("x-telegram-bot-api-secret-token", "")
        if not hmac.compare_digest(got.encode(), secret.encode()):
            return "bad_secret"
        uid = update.get("update_id")
        if not isinstance(uid, int) or uid <= st.last_update_id:
            return "replay"
        st.last_update_id = uid
        msg = update.get("message")
        if not isinstance(msg, dict):
            return "ignored"
        frm, chat = msg.get("from") or {}, msg.get("chat") or {}
        if frm.get("is_bot") or "forward_origin" in msg or "forward_date" in msg or frm.get("id") != chat.get("id"):
            return "ignored"
        d = msg.get("date")
        if isinstance(d, int) and now - d > 300:
            return "stale"
        r = self._pairing().claim(frm.get("id"), chat.get("type"), msg.get("text"), now=now)
        return "paired" if r == "paired" else "ignored"
