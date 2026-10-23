from telegram_pairing import Pairing

def test_pair_ok_and_single_use():
    p = Pairing(); c = p.start(now=0)
    assert p.claim(42, "private", f"/pair {c}", now=1) == "paired" and p.bound_id == 42
    assert p.claim(99, "private", f"/pair {c}", now=2) == "rejected" and p.bound_id == 42

def test_bare_id_or_first_message_never_binds():
    p = Pairing(); p.start(now=0)
    assert p.claim(42, "private", "/start", now=1) == "rejected" and p.bound_id is None
    assert Pairing().claim(42, "private", "/pair ABCDEFGH") == "rejected"

def test_wrong_code_locks_after_five():
    p = Pairing(); c = p.start(now=0)
    for _ in range(5): assert p.claim(7, "private", "/pair WRONGWRO", now=1) == "rejected"
    assert p.claim(42, "private", f"/pair {c}", now=2) == "rejected" and p.bound_id is None

def test_expiry():
    p = Pairing(ttl=60); c = p.start(now=0)
    assert p.claim(42, "private", f"/pair {c}", now=61) == "rejected" and p.bound_id is None

def test_group_chat_rejected():
    p = Pairing(); c = p.start(now=0)
    assert p.claim(42, "group", f"/pair {c}", now=1) == "rejected"

def test_rebind_replaces_atomically_and_audits():
    ev = []; p = Pairing(audit=ev.append)
    c1 = p.start(now=0); p.claim(42, "private", f"/pair {c1}", now=1)
    c2 = p.start(now=10); assert p.bound_id == 42
    assert p.claim(77, "private", f"/pair {c2.lower()}", now=11) == "paired" and p.bound_id == 77
    assert {"event": "paired", "replaced": True} in ev

def test_new_code_replaces_old_and_revoke():
    p = Pairing(); c1 = p.start(now=0); c2 = p.start(now=1)
    assert p.claim(1, "private", f"/pair {c1}", now=2) == "rejected"
    assert p.claim(1, "private", f"/pair {c2}", now=2) == "paired"
    p.revoke(); assert p.bound_id is None
