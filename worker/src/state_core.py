"""Single source of truth for Maya's Telegram security state, as plain JSON-able data.

Runs inside ONE Cloudflare Durable Object (single-threaded, strongly consistent), so the replay guard, rate limit,
pairing lockout and On/Off switch cannot race across Worker isolates. Pure logic: no network, no Cloudflare imports.
The caller persists `core.data` after every call.
"""
import hmac, os, time
from telegram_channel import State, handle
from telegram_pairing import Pairing


# Free-eligible Cloudflare Workers AI text models selectable as Maya's answering model (2026-10-24 pricing page).
# Eligibility is re-verified live after deploy; if a model starts erroring, its replies fall back honestly.
# Workers AI IDs RUNTIME-VERIFIED 2026-10-24 via a scoped local binding probe
# (wrangler dev --remote, real AI binding, no public endpoint, tiny inferences
# well inside the 10k neurons/day free allocation) on the staging account:
# every ID below returned content. gemma-3-12b-it was REMOVED (error 5018:
# account not allowed). gpt-oss/qwen3.8/gemma-4 answer via chat-completion
# shape; worker/src/entry.py extracts both response shapes.
WORKER_MODELS = {
    "@cf/meta/llama-3.1-8b-instruct-fp8-fast": "Llama 3.1 8B",
    "@cf/qwen/qwen3-30b-a3b-fp8": "Qwen3 30B",
    "@cf/meta/llama-3.3-70b-instruct-fp8-fast": "Llama 3.3 70B",
    "@cf/openai/gpt-oss-120b": "GPT-OSS 120B",
    "@cf/qwen/qwen3.8-27b": "Qwen 3.8 27B",
    "@cf/google/gemma-4-26b-a4b-it": "Gemma 4 26B",
}
DEFAULT_WORKER_MODEL = "@cf/meta/llama-3.1-8b-instruct-fp8-fast"

# Image engines (both runtime-verified on the live whos-s4hill account binding 2026-10-24).
# DEFAULT = Sahil's explicit choice 2026-10-24 ("Ye best h" on the klein-4b sample vs schnell).
# FALLBACK keeps the prior engine; when the fallback fires, the reply/API must name
# the ACTUAL engine used - never label fallback output as the default engine's.
DEFAULT_IMAGE_MODEL = "@cf/black-forest-labs/flux-2-klein-4b"
FALLBACK_IMAGE_MODEL = "@cf/black-forest-labs/flux-1-schnell"
IMAGE_ENGINE_NAMES = {
    DEFAULT_IMAGE_MODEL: "FLUX.2 Klein 4B",
    FALLBACK_IMAGE_MODEL: "FLUX.1 Schnell",
}

# OpenRouter free-pool models (every ID revalidated against live catalog https://openrouter.ai/api/v1/models 2026-10-24 (pricing 0/0) AND adapter-tested live; inkling:free removed (403: agentic-harness-gated, unreachable via plain API)).
# PUBLIC POOL: inputs pass to third-party model providers who may train on them (openrouter.ai/privacy).
# These are NEVER in the auto chain, receive only the current prompt (no history/memory/screen/voice),
# and secret-looking prompts are refused before sending. The settings UI must show the non-private warning.
OPENROUTER_MODELS = {
    "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free": "Nemotron 3 Ultra (free pool - NON-PRIVATE questions only)",
    "openrouter/nvidia/nemotron-3.5-super-vl:free": "Nemotron 3.5 Super VL (free pool - NON-PRIVATE questions only)",
}

GROQ_MODELS = {
    "groq/openai/gpt-oss-120b": "GPT-OSS 120B via Groq (free pool - NON-PRIVATE questions only)",
    "groq/openai/gpt-oss-20b": "GPT-OSS 20B via Groq (free pool - NON-PRIVATE questions only)",
    "groq/qwen/qwen3.8-27b": "Qwen 3.8 27B via Groq (free pool - NON-PRIVATE questions only)",
}

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

    # -- Maya per-minute burst limit (Maya's own, NOT a provider quota). One counter per bucket per UTC minute.
    def rl_hit(self, bucket, limit, now=None):
        """Increment and check bucket for the current minute. Returns (allowed, retry_after_seconds)."""
        now = time.time() if now is None else now
        rl = self.data.setdefault("rl", {})
        minute = int(now // 60)
        b = rl.get(bucket)
        if not isinstance(b, dict) or b.get("m") != minute:
            b = {"m": minute, "n": 0}
        b["n"] += 1
        rl[bucket] = b
        if b["n"] > limit:
            return False, max(1, 60 - int(now % 60))
        return True, 0

    # -- owner actions (caller must have authenticated the Maya owner)
    MODEL_PREFS = ("auto", "backup_only")

    @property
    def model_pref(self):
        v = self.data.get("model_pref", "auto")
        return v if v in self.MODEL_PREFS or v in WORKER_MODELS or v in OPENROUTER_MODELS or v in GROQ_MODELS else "auto"

    def set_model_pref(self, v):
        if v not in self.MODEL_PREFS and v not in WORKER_MODELS and v not in OPENROUTER_MODELS and v not in GROQ_MODELS:
            raise ValueError("bad_model_pref")
        self.data["model_pref"] = v

    def set_enabled(self, on: bool):
        self.data["tg"]["owner_enabled"] = bool(on)
        self._audit({"event": "toggle", "on": bool(on)})

    # -- Telegram voice conversation mode: when on, every normal chat answer is ALSO spoken (TTS audio).
    @property
    def voice_mode(self):
        return bool(self.data.get("voice_mode"))

    def set_voice_mode(self, on: bool):
        self.data["voice_mode"] = bool(on)
        self._audit({"event": "voice_mode", "on": bool(on)})

    # -- Telegram image mode: when on, an owner photo+caption is an EDIT request; ordinary text stays chat.
    @property
    def image_mode(self):
        return bool(self.data.get("image_mode"))

    def set_image_mode(self, on: bool):
        self.data["image_mode"] = bool(on)
        self._audit({"event": "image_mode", "on": bool(on)})

    # -- failure ring buffer: metadata only (stage + status + ts), NEVER content/audio/transcripts.
    FAIL_CAP = 20

    def fail_add(self, stage: str, status: str):
        fails = self.data.setdefault("fails", [])
        fails.append({"ts": int(time.time()), "stage": str(stage)[:40], "status": str(status)[:60]})
        del fails[:-self.FAIL_CAP]

    FAIL_TTL_S = 7 * 24 * 3600  # metadata retention bound: entries older than 7 days drop out

    def fail_get(self, n: int = 10):
        cutoff = int(time.time()) - self.FAIL_TTL_S
        return [f for f in self.data.get("fails", []) if int(f.get("ts", 0)) >= cutoff][-int(n):]

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
        return status, text, (bound if status in ("ok", "voice", "photo") else None)

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
