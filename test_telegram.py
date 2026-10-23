import os
from telegram_channel import handle, State

H = {"X-Telegram-Bot-Api-Secret-Token": "s3cret_-"}

def upd(uid=1, fid=42, cid=42, text="hi", ctype="private", **extra):
    m = {"from": {"id": fid, "is_bot": False}, "chat": {"id": cid, "type": ctype}, "text": text}
    m.update(extra)
    return {"update_id": uid, "message": m}

def on(**kw):
    os.environ.update(MAYA_TELEGRAM_ENABLED="1", MAYA_TELEGRAM_SECRET="s3cret_-", MAYA_TELEGRAM_OWNER_ID="42")
    os.environ.pop("MAYA_TELEGRAM_RATE", None)
    os.environ.update(kw)

def test_off_by_default():
    for k in ("MAYA_TELEGRAM_ENABLED", "MAYA_TELEGRAM_SECRET", "MAYA_TELEGRAM_OWNER_ID"): os.environ.pop(k, None)
    assert handle(H, upd(), State())[0] == "disabled"

def test_owner_ok():
    on(); assert handle(H, upd(text="  call mom "), State()) == ("ok", "call mom")

def test_wrong_or_missing_secret():
    on(); assert handle({}, upd(), State())[0] == "bad_secret"
    assert handle({"X-Telegram-Bot-Api-Secret-Token": "nope"}, upd(), State())[0] == "bad_secret"

def test_stranger_group_forward_bot_ignored():
    on()
    assert handle(H, upd(fid=7, cid=7), State())[0] == "ignored"
    assert handle(H, upd(ctype="group", cid=-100), State())[0] == "ignored"
    assert handle(H, upd(forward_origin={"type": "user"}), State())[0] == "ignored"
    assert handle(H, upd(fid=42, cid=99), State())[0] == "ignored"
    assert handle(H, {"update_id": 1, "channel_post": {}}, State())[0] == "ignored"

def test_replay_and_order():
    on(); s = State()
    assert handle(H, upd(uid=5), s)[0] == "ok"
    assert handle(H, upd(uid=5), s)[0] == "replay"
    assert handle(H, upd(uid=4), s)[0] == "replay"
    assert handle(H, upd(uid=6), s)[0] == "ok"

def test_rate_limit_and_window():
    on(MAYA_TELEGRAM_RATE="2"); s = State()
    assert handle(H, upd(uid=1), s, now=100)[0] == "ok"
    assert handle(H, upd(uid=2), s, now=101)[0] == "ok"
    assert handle(H, upd(uid=3), s, now=102)[0] == "rate_limited"
    assert handle(H, upd(uid=3), s, now=170)[0] == "ok"

def test_text_cap_and_empty():
    on(); assert len(handle(H, upd(text="x" * 5000), State())[1]) == 2000
    assert handle(H, upd(text="   "), State())[0] == "ignored"
