import pytest
from github_tools import *

def test_allowed_and_protected():
    assert parse_command("/gh read whoss4hill/maya-app local-fixes README.md")["op"] == "read"
    assert parse_command("/gh edit whoss4hill/maya-app main a.kt: x")["why"] == "protected_branch"
    assert parse_command("/gh edit someone/else dev a.kt: x")["why"] == "repo_not_allowed"
    assert parse_command("/gh edit whoss4hill/maya-app dev ../a: x")["why"] in ("bad_path",)
    assert parse_command("/gh edit whoss4hill/maya-app dev .github/workflows/x.yml: x")["why"] == "path_blocked"
    assert parse_command("/gh edit whoss4hill/maya-app dev local.properties: x")["why"] == "path_blocked"
    assert parse_command("/gh edit whoss4hill/maya-app dev a.kt")["why"] == "missing_instruction"

def test_plan_and_url():
    c = parse_command("/gh edit whoss4hill/maya-backend cloudflare-port edge.py: fix typo")
    assert "NOT pushed" in plan_preview(c)
    assert contents_url("whoss4hill/maya-backend", "cloudflare-port", "edge.py").endswith("edge.py?ref=cloudflare-port")
    with pytest.raises(ValueError): contents_url("x/y", "b", "p")


def test_read_write_helpers():
    import base64
    from github_tools import parse_read, write_body, api_headers, confirm_matches
    assert parse_read({"type": "file", "size": 5, "content": base64.b64encode(b"hello").decode()}) == "hello"
    assert parse_read({"type": "dir"}) == "Not a single file."
    assert parse_read({"type": "file", "size": 999999, "content": ""}) == "File too large to show."
    b = write_body("whoss4hill/maya-backend", "cloudflare-port", "edge.py", "x", "m", "abc")
    assert b["branch"] == "cloudflare-port" and b["sha"] == "abc" and base64.b64decode(b["content"]) == b"x"
    with pytest.raises(ValueError): write_body("whoss4hill/maya-backend", "main", "edge.py", "x", "m")
    with pytest.raises(ValueError): write_body("other/repo", "dev", "edge.py", "x", "m")
    assert api_headers("T")["Authorization"] == "Bearer T"
    assert confirm_matches({"used": False}, " Yes Push ") and not confirm_matches({"used": True}, "yes push") and not confirm_matches({}, "yes push")
