import json
import test_edge as T
from test_edge import app  # noqa: F401

class FakeAI:
    def __init__(self, out): self.out, self.calls = out, []
    async def image(self, p): self.calls.append(p); return self.out

def post(app, body): return T.run(app.handle("POST", "/v1/image", T.A, json.dumps(body)))

def test_image_ok(app):
    app.ai = FakeAI("QUJD"); s, b = post(app, {"prompt": "a red kite over a hill"})
    assert s == 200 and b["image_base64"] == "QUJD" and b["mime"] == "image/jpeg"

def test_image_not_configured(app):
    s, b = post(app, {"prompt": "a kite"}); assert s == 501

def test_image_refuses_secret_without_calling_ai(app):
    app.ai = FakeAI("QUJD"); s, b = post(app, {"prompt": "draw my password is Hunter22abcd"})
    assert s == 422 and not app.ai.calls

def test_image_validation_and_failure(app):
    app.ai = FakeAI(None)
    assert post(app, {"prompt": ""})[0] == 400
    assert post(app, {"prompt": "x" * 401})[0] == 400
    assert post(app, {"prompt": "a kite"})[0] == 502

def test_image_requires_jwt(app):
    s, b = T.run(app.handle("POST", "/v1/image", {}, json.dumps({"prompt": "a kite"}))); assert s == 401
