"""Best-effort detector for obvious credential payloads (private keys, card numbers, well-known API token shapes,
"password is ..." style statements). It is a safety net, NOT a promise of perfect detection: unusual secrets can pass.
Used to refuse saving them as memories / chat history and to keep them away from the model."""
import re

_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}\b"),                       # Google API key
    re.compile(r"\b(?:sk|pk|rk)[-_](?:live|test|proj|ant)?[-_]?[A-Za-z0-9_\-]{20,}\b"),  # sk-..., Stripe/OpenAI/Anthropic style
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"), re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}\b"),                  # Slack
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                               # AWS access key id
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_\-]{35}\b"),                    # Telegram bot token
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}\b"),  # JWT
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9_\-\.=]{20,}"),
    re.compile(r"(?i)\b(?:password|passcode|passwd|pwd|pin|cvv|cvc|otp|api[ _-]?key|secret|token)\b\s*(?:is|=|:|hai|h)\s*[\"']?[^\s\"']{4,}"),
    re.compile(r"(?i)\b(?:cvv|cvc|pin)\b\s*(?:is|=|:|hai|h)?\s*\d{3,8}\b"),
    re.compile(r"(?i)\b(?:mera|my)\s+(?:password|pin|cvv)\b\s*[^\s]{0,3}\s*[\"']?[^\s\"']{4,}"),
]
_CARD = re.compile(r"(?<![\d])(?:\d[ -]?){13,19}(?![\d])")


def _luhn(digits):
    s, alt = 0, False
    for ch in reversed(digits):
        d = ord(ch) - 48
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        s += d
        alt = not alt
    return s % 10 == 0


def looks_like_secret(text):
    t = text or ""
    if not t.strip():
        return False
    for p in _PATTERNS:
        if p.search(t):
            return True
    for m in _CARD.finditer(t):
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn(digits):
            return True
    return False


NOTICE = ("That looks like a password, key or card number, so I did not save it or send it to the AI. "
          "Keep secrets in the Vault, not in chat or memories.")
