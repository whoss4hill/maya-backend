"""Pairing/rebinding of the Telegram account Maya obeys (logic only; storage is an injected dict; not wired/live).

A bare Telegram chat id is NEVER authority. Binding changes only when both sides prove themselves:
 1. the authenticated Maya owner asks for a pairing code (caller must have passed owner auth);
 2. the Telegram account to be bound sends `/pair <code>` to the bot in a private chat;
 3. the code is single-use, expires (default 10 min), is stored only as a hash, and a new request
    replaces any open one; wrong attempts are counted and lock pairing after 5 failures until a new code is made.
On success the previous binding is replaced in the same step (no moment with two owners) and the event is audited.
revoke() clears the binding. The first message from an unknown account never binds anything.
"""
import hashlib, hmac, secrets, time

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _h(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


class Pairing:
    def __init__(self, store: dict = None, audit=lambda e: None, ttl: int = 600):
        self.s = store if store is not None else {}
        self.audit, self.ttl = audit, ttl

    @property
    def bound_id(self):
        return self.s.get("bound_id")

    def start(self, now: float = None) -> str:
        """Call only after the Maya owner is authenticated. Returns the code to show the owner in the app."""
        code = "".join(secrets.choice(ALPHABET) for _ in range(8))
        self.s["pending"] = {"hash": _h(code), "exp": (time.time() if now is None else now) + self.ttl, "fails": 0}
        self.audit({"event": "pair_started"})
        return code

    def claim(self, telegram_id: int, chat_type: str, text: str, now: float = None) -> str:
        """Handle a message `/pair CODE` from a Telegram account. Returns paired | rejected."""
        now = time.time() if now is None else now
        p = self.s.get("pending")
        parts = (text or "").strip().split()
        if chat_type != "private" or len(parts) != 2 or parts[0].lower() != "/pair" or not p:
            return "rejected"
        if now > p["exp"] or p["fails"] >= 5:
            self.s.pop("pending", None)
            self.audit({"event": "pair_expired_or_locked"})
            return "rejected"
        if not hmac.compare_digest(_h(parts[1].strip().upper()), p["hash"]):
            p["fails"] += 1
            self.audit({"event": "pair_bad_code"})
            return "rejected"
        old = self.s.get("bound_id")
        self.s["bound_id"] = int(telegram_id)
        self.s.pop("pending", None)
        self.audit({"event": "paired", "replaced": old is not None})
        return "paired"

    def revoke(self):
        self.s.pop("bound_id", None)
        self.s.pop("pending", None)
        self.audit({"event": "revoked"})
