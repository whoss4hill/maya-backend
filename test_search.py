import os, sys
os.environ.update(SUPABASE_URL="https://x.supabase.co", SUPABASE_SERVICE_ROLE_KEY="k", GEMINI_API_KEY="g", MAYA_OWNER_USER_ID="aaa")

def load(**env):
    for k in ("MAYA_WEB_SEARCH", "MAYA_SEARCH_MONTHLY_CAP"):
        os.environ.pop(k, None)
    os.environ.update(env)
    sys.modules.pop("main", None)
    import main
    return main

def test_tool_enabled_by_default():
    m = load()
    assert m._gemini_payload("hi")["tools"] == [{"google_search": {}}]

def test_switch_off():
    m = load(MAYA_WEB_SEARCH="0")
    assert "tools" not in m._gemini_payload("hi")

def test_cap_turns_tool_off_and_counts_queries():
    m = load(MAYA_SEARCH_MONTHLY_CAP="2")
    data = {"candidates": [{"groundingMetadata": {"webSearchQueries": ["a", "b", " "], "groundingChunks": []}}]}
    m._search_record(data)
    assert m._search_used["n"] == 2
    assert "tools" not in m._gemini_payload("hi")

def test_sources_dedup_and_limit():
    m = load()
    chunks = [{"web": {"uri": f"https://u/{i % 4}", "title": f"t{i % 4}"}} for i in range(12)]
    src = m._search_record({"candidates": [{"groundingMetadata": {"groundingChunks": chunks}}]})
    assert len(src) == 4 and len({u for _, u in src}) == 4

def test_reply_with_and_without_sources():
    m = load()
    assert m._with_sources("ok", []) == "ok"
    out = m._with_sources("ok", [("t", "https://u")])
    assert out.startswith("ok\n\nSources:\n") and "- t: https://u" in out

def test_malformed_response_is_safe():
    m = load()
    assert m._search_record({}) == [] and m._search_record({"candidates": []}) == []
