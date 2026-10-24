import asyncio, json, pytest
from secret_guard import looks_like_secret as L
import test_edge as T

# Synthetic samples built at runtime (no real credentials in the repo).
POS = [
    "-----BEGIN " + "RSA PRIVATE KEY-----\nabc",
    "my key is AI" + "za" + "A" * 35,
    "token ghp" + "_" + "a" * 36,
    "card 4111 1111 1111 1111 exp 12/30",
    "mera password hai Hunter2xyz",
    "password: correct-horse-battery",
    "cvv 123",
    "use Bearer " + "a" * 30 + " for it",
    "bot " + "1234567890:" + "A" * 35,
    "password is $ecret99x",
    "password: (hunter22abc)",
    "password = <Hunter22abc",
    "my pin is [4821]",
    "password: {hunter2xyz}",
    "password: %ecret99%",
    "password: <" + "A1b2C3d4E5f6>",
    "token: $" + "ghp" + "_" + "a" * 36,
]
NEG = [
    "I take 3 pins and 123 pens",
    "pin the message to the top",
    "remember that I like tea without sugar",
    "call mom at 9876543210",
    "my order number is 1234567890123 (not a card)",
    "I forgot my password, how do I reset it?",
    "set a reminder for 5 pm",
    "Hello golu mere jaan kaise ho",
    "the token ring network is old",
    "Use the password field in the form: <input type=\"password\" name=\"pwd\">",
    "Help: to reset, click Forgot password on the login page",
    "In Python write x = os.environ[\"API_KEY\"] instead of hardcoding it",
    "curl -H \"Authorization: Bearer $TOKEN\" https://example.com/api",
    "git config user.password is not a thing, use a credential helper",
    "The API key goes in the header named X-Api-Key",
    "def check_password(user, password): return hash(password) == user.hash",
    "Don't share your password or CVV with anyone",
    "```\nexport PASSWORD=<your-password>\n```",
]

@pytest.mark.parametrize("t", POS)
def test_detects(t): assert L(t)

@pytest.mark.parametrize("t", NEG)
def test_leaves_normal_text(t): assert not L(t)

def test_chat_with_secret_not_stored_nor_sent_to_model(app):
    s, b = T.run(app.handle("POST", "/v1/chat", T.A, json.dumps({"message": "password: correct-horse-battery"})))
    assert s == 200 and "Vault" in b["reply"]
    assert not any("generateContent" in u or "/conversations" in u for _, u, _ in app.http.calls)

def test_memory_with_secret_refused(app):
    s, b = T.run(app.handle("POST", "/v1/memories", T.A, json.dumps({"kind": "fact", "content": "card 4111 1111 1111 1111"})))
    assert s == 422
    assert not any("/rest/v1/memories" in u and m == "POST" for m, u, _ in app.http.calls)

app = T.app
