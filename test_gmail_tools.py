import pytest
from gmail_tools import *

def test_commands():
    assert parse_command("/mail")["q"].startswith("is:unread")
    assert parse_command("/mail from alice")["q"] == 'from:"alice" in:inbox'
    assert parse_command('/mail from a"b')["op"] == "error"
    assert parse_command("/mail send to bob@x.com: hi there")["op"] == "draft"
    assert parse_command("/mail send to notanemail: hi")["why"] == "bad_recipient"
    assert parse_command("hello") is None

def test_draft_never_sends_and_urls_safe():
    d = parse_command("/mail send to bob@x.com: hi")
    assert "NOT sent" in draft_preview(d)
    with pytest.raises(ValueError): get_url("../../x")
    assert "q=is%3Aunread" in list_url("is:unread")
    assert "readonly" in SCOPE_READ

def test_render():
    m = {"snippet": "Hello  world", "payload": {"headers": [{"name": "From", "value": "A <a@b.com>"}, {"name": "Subject", "value": "S"}]}}
    assert "A <a@b.com> | S" in render([m]) and "nahi" in render([])
