import os
from tools_gate import Gate, Tool

def make():
    sent = []
    g = Gate(audit=lambda e: sent.append(("audit", e["status"])))
    g.register(Tool("email_send", {"to": str, "body": str}, True, lambda to, body: sent.append((to, body)) or "sent"))
    g.register(Tool("web_note", {"q": str}, False, lambda q: "noted"))
    return g, sent

def setup_function(_):
    for k in list(os.environ):
        if k.startswith("MAYA_TOOL_"): del os.environ[k]

def test_off_by_default():
    g, sent = make()
    assert g.call("email_send", {"to": "a@b.c", "body": "hi"}, owner_said={"to": "a@b.c", "body": "hi"})["status"] == "disabled"
    assert not [s for s in sent if s[0] != "audit"]

def test_unknown_and_bad_args():
    g, _ = make(); os.environ["MAYA_TOOL_EMAIL_SEND"] = "1"
    assert g.call("nope", {})["status"] == "unknown_tool"
    assert g.call("email_send", {"to": "a@b.c"})["status"] == "bad_arguments"
    assert g.call("email_send", {"to": "a@b.c", "body": ""})["status"] == "bad_arguments"
    assert g.call("email_send", {"to": "a@b.c", "body": "x", "cc": "z"})["status"] == "bad_arguments"

def test_model_drafted_args_need_confirmation_then_run():
    g, sent = make(); os.environ["MAYA_TOOL_EMAIL_SEND"] = "1"
    r = g.call("email_send", {"to": "a@b.c", "body": "drafted"}, owner_said={"to": "a@b.c"})
    assert r["status"] == "needs_confirmation" and ("a@b.c", "drafted") not in sent
    assert g.call("email_send", {"to": "a@b.c", "body": "drafted"}, confirmed_key=r["key"])["status"] == "ok"
    assert ("a@b.c", "drafted") in sent

def test_changed_argument_invalidates_confirmation():
    g, sent = make(); os.environ["MAYA_TOOL_EMAIL_SEND"] = "1"
    r = g.call("email_send", {"to": "a@b.c", "body": "one"})
    assert g.call("email_send", {"to": "a@b.c", "body": "two"}, confirmed_key=r["key"])["status"] == "needs_confirmation"

def test_fully_stated_owner_command_runs():
    g, sent = make(); os.environ["MAYA_TOOL_EMAIL_SEND"] = "1"
    a = {"to": "a@b.c", "body": "hello"}
    assert g.call("email_send", a, owner_said=dict(a))["status"] == "ok"

def test_no_confirm_tool_still_needs_enable():
    g, _ = make()
    assert g.call("web_note", {"q": "x"})["status"] == "disabled"
    os.environ["MAYA_TOOL_WEB_NOTE"] = "1"
    assert g.call("web_note", {"q": "x"})["status"] == "ok"
