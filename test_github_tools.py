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
