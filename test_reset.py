import os, sys
os.environ.update(SUPABASE_URL="https://x.supabase.co", SUPABASE_SERVICE_ROLE_KEY="SERVICEKEY", GEMINI_API_KEY="g", MAYA_OWNER_USER_ID="aaa")

def load(anon):
    os.environ["SUPABASE_ANON_KEY"] = anon
    sys.modules.pop("main", None)
    import main
    return main

def test_reset_page_unconfigured_503():
    from fastapi.testclient import TestClient
    r = TestClient(load("").app).get("/auth/reset"); assert r.status_code == 503

def test_reset_page_ok_no_service_key_and_safe_headers():
    from fastapi.testclient import TestClient
    r = TestClient(load("ANONPUB").app).get("/auth/reset")
    assert r.status_code == 200
    assert "ANONPUB" in r.text and "https://x.supabase.co" in r.text
    assert "SERVICEKEY" not in r.text
    assert r.headers["cache-control"] == "no-store" and r.headers["referrer-policy"] == "no-referrer"
    assert "history.replaceState" in r.text and "/auth/v1/user" in r.text
