import os, json, time
os.environ.update(MAYA_TELEGRAM_ENABLED="1", MAYA_TELEGRAM_SECRET="s3cret")
import pytest
from state_core import Core


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("MAYA_TELEGRAM_ENABLED", "1"); monkeypatch.setenv("MAYA_TELEGRAM_SECRET", "s3cret")
H = {"X-Telegram-Bot-Api-Secret-Token": "s3cret"}

def upd(i, uid, text, now, chat_type="private", cid=None):
    cid = uid if cid is None else cid
    return {"update_id": i, "message": {"from": {"id": uid}, "chat": {"id": cid, "type": chat_type}, "text": text, "date": int(now)}}

def roundtrip(c): return Core(json.loads(json.dumps(c.data)))  # persisted between calls like the DO does

def test_pairing_then_command_survive_persistence():
    now = time.time(); c = Core(); code = c.pair_start(now)
    assert c.telegram_update(H, upd(1, 1234567890, "/pair WRONG", now), now)[0] == "ignored"
    c = roundtrip(c)
    assert c.telegram_update(H, upd(2, 111, f"/pair {code}", now), now)[0] == "paired" and c.bound_id == 111
    c = roundtrip(c)
    assert c.telegram_update(H, upd(3, 111, "hello", now), now) == ("ok", "hello", 111)
    assert c.telegram_update(H, upd(3, 111, "hello", now), now)[0] == "replay"      # replay survives persistence
    assert c.telegram_update(H, upd(4, 222, "hi", now), now) == ("ignored", None, None)   # stranger: no reply

def test_lockout_after_five_bad_codes():
    now = time.time(); c = Core(); code = c.pair_start(now)
    for i in range(1, 6):
        c = roundtrip(c); c.telegram_update(H, upd(i, 5, "/pair BADBADBA", now), now)
    c = roundtrip(c)
    assert c.telegram_update(H, upd(9, 5, f"/pair {code}", now), now)[0] == "ignored" and c.bound_id is None

def test_off_consumes_and_stale_dropped_after_reenable():
    now = time.time(); c = Core(); code = c.pair_start(now); c.telegram_update(H, upd(1, 7, f"/pair {code}", now), now)
    c = roundtrip(c); c.set_enabled(False)
    assert c.telegram_update(H, upd(2, 7, "do it", now), now) == ("off", None, None)
    c = roundtrip(c); c.set_enabled(True)
    assert c.telegram_update(H, upd(2, 7, "do it", now), now)[0] == "replay"         # cannot run after re-enable
    assert c.telegram_update(H, upd(3, 7, "old", now - 1000), now)[0] == "stale"

def test_bad_secret_and_rebind_replaces_and_revoke():
    now = time.time(); c = Core(); code = c.pair_start(now)
    assert c.telegram_update({"X-Telegram-Bot-Api-Secret-Token": "x"}, upd(1, 7, f"/pair {code}", now), now)[0] == "bad_secret"
    c.telegram_update(H, upd(2, 7, f"/pair {code}", now), now); assert c.bound_id == 7
    code2 = c.pair_start(now); c.telegram_update(H, upd(3, 8, f"/pair {code2}", now), now); assert c.bound_id == 8
    assert c.telegram_update(H, upd(4, 7, "x", now), now)[2] is None
    c.revoke(); assert c.bound_id is None and c.telegram_update(H, upd(5, 8, "x", now), now)[2] is None

def test_group_chat_cannot_pair():
    now = time.time(); c = Core(); code = c.pair_start(now)
    assert c.telegram_update(H, upd(1, 9, f"/pair {code}", now, chat_type="group", cid=-5), now)[0] == "ignored" and c.bound_id is None


def test_targeted_pairing_rejects_other_chat_and_accepts_target():
    from telegram_pairing import Pairing
    s = {}; p = Pairing(s)
    code = p.start(now=1000, target_id=555)
    assert p.claim(999, "private", "/pair " + code, now=1001) == "rejected" and p.bound_id is None
    assert p.claim(555, "private", "/pair " + code, now=1002) == "paired" and p.bound_id == 555
    c2 = p.start(now=2000)  # untargeted still works
    assert p.claim(777, "private", "/pair " + c2, now=2001) == "paired"
