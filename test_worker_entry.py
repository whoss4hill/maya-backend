"""Regression tests for the REAL Worker entry adapter (worker/src/entry.py).

Guards the Oct 24 ebb0a93 regression where a model_pref refactor silently deleted
the _AI binding class and the `app.ai = _AI(self.env)` wiring, breaking /v1/image,
Telegram /image and the Workers AI backup text on the live Worker.

The `workers` / `js` / `pyodide` runtime modules are stubbed so the genuine entry
module imports and runs under plain pytest.
"""
import asyncio, importlib.util, json, sys, types
from pathlib import Path

import pytest


def _stub_runtime():
    w = types.ModuleType("workers")

    class WorkerEntrypoint:
        pass

    class DurableObject:
        def __init__(self, ctx=None, env=None):
            self.ctx, self.env = ctx, env

    class Response:
        def __init__(self, body="", status=200, headers=None):
            self.body, self.status, self.headers = body, status, headers or {}

    w.WorkerEntrypoint, w.DurableObject, w.Response = WorkerEntrypoint, DurableObject, Response
    sys.modules["workers"] = w

    js = types.ModuleType("js")

    class Object:
        @staticmethod
        def fromEntries(it):
            return dict(it)

    class FormData:
        def __init__(self):
            self.fields = []

        @staticmethod
        def new():
            return FormData()

        def append(self, k, v):
            self.fields.append((k, v))

    class _JsHeaders:
        def get(self, k):
            assert k == "content-type"
            return "multipart/form-data; boundary=----test"

    class JsResponse:
        def __init__(self, form):
            self.body = ("STREAM", list(form.fields))
            self.headers = _JsHeaders()

        @staticmethod
        def new(form):
            return JsResponse(form)

    js.Object, js.FormData, js.Response = Object, FormData, JsResponse
    sys.modules["js"] = js

    pyodide = types.ModuleType("pyodide")
    ffi = types.ModuleType("pyodide.ffi")
    ffi.to_js = lambda x, dict_converter=None: x
    pyodide.ffi = ffi
    sys.modules["pyodide"] = pyodide
    sys.modules["pyodide.ffi"] = ffi


def _load_entry():
    _stub_runtime()
    path = Path(__file__).parent / "worker" / "src" / "entry.py"
    spec = importlib.util.spec_from_file_location("maya_worker_entry", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _RunResult:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeAIBinding:
    def __init__(self, result=None, raises=False):
        self.calls, self.result, self.raises = [], result, raises

    async def run(self, model, payload):
        self.calls.append((model, payload))
        if self.raises:
            raise RuntimeError("boom")
        return self.result


class _FakeStateNS:
    def idFromName(self, name):
        return name

    def get(self, _id):
        return object()


class _FakeEnv:
    def __init__(self, ai):
        self.AI = ai
        self.MAYA_STATE = _FakeStateNS()


class _FakeHeaders:
    def get(self, _k):
        return None


class _FakeRequest:
    def __init__(self, url, method="POST", body=""):
        self.url, self.method, self._body = url, method, body
        self.headers = _FakeHeaders()

    async def text(self):
        return self._body


class _CapturingApp:
    """Stands in for edge.App; records construction kwargs and assigned attrs."""
    last = None

    def __init__(self, **kw):
        self.kw = kw
        _CapturingApp.last = self

    async def handle(self, method, path, headers, body):
        return 200, {"reply": "ok"}


@pytest.fixture()
def entry():
    return _load_entry()


def test_ai_adapter_image_default_is_user_chosen_klein4b(entry):
    ai = FakeAIBinding(result=_RunResult(image="QUJD"))
    out = asyncio.run(entry._AI(_FakeEnv(ai)).image("a red kite"))
    assert out == "QUJD"
    model, payload = ai.calls[0]
    assert model == "@cf/black-forest-labs/flux-2-klein-4b"  # user-chosen default 2026-10-24
    mp = payload["multipart"]
    assert mp["contentType"].startswith("multipart/form-data; boundary=")
    assert ("prompt", "a red kite") in mp["body"][1]

def test_ai_adapter_image_explicit_schnell_uses_plain_shape(entry):
    ai = FakeAIBinding(result=_RunResult(image="QUJD"))
    asyncio.run(entry._AI(_FakeEnv(ai)).image("kite", model="@cf/black-forest-labs/flux-1-schnell"))
    assert ai.calls[0][0] == "@cf/black-forest-labs/flux-1-schnell"
    assert ai.calls[0][1] == {"prompt": "kite", "steps": 4}


def test_ai_adapter_text_uses_llama_backup(entry):
    ai = FakeAIBinding(result=_RunResult(response="backup says hi"))
    out = asyncio.run(entry._AI(_FakeEnv(ai)).text("hello"))
    assert out == "backup says hi"
    assert ai.calls[0][0] == "@cf/meta/llama-3.1-8b-instruct-fp8-fast"
    msgs = ai.calls[0][1]["messages"]
    assert msgs[-1] == {"role": "user", "content": "hello"}


def test_ai_adapter_text_extracts_chat_completion_shape(entry):
    # gpt-oss/qwen3.8/gemma-4 on Workers AI return OpenAI chat-completion
    # shape (choices[0].message.content) instead of {"response": ...}
    msg = types.SimpleNamespace(content="\n\nOK", reasoning_content="chain")
    ai = FakeAIBinding(result=_RunResult(choices=[types.SimpleNamespace(message=msg)]))
    out = asyncio.run(entry._AI(_FakeEnv(ai)).text("hi"))
    assert out == "OK"


def test_ai_adapter_failures_return_none(entry):
    ai = FakeAIBinding(raises=True)
    a = entry._AI(_FakeEnv(ai))
    assert asyncio.run(a.image("x")) is None
    assert asyncio.run(a.text("x")) is None


def test_fetch_wires_ai_binding_onto_app(entry, monkeypatch):
    import edge
    monkeypatch.setattr(edge, "App", _CapturingApp)
    ai = FakeAIBinding(result=_RunResult(image="QUJD"))
    d = object.__new__(entry.Default)
    d.env, d.ctx = _FakeEnv(ai), types.SimpleNamespace(waitUntil=lambda c: None)
    req = _FakeRequest("https://maya-backend-staging.whos-s4hill.workers.dev/v1/chat",
                       method="POST", body=json.dumps({"message": "hi"}))
    resp = asyncio.run(d.fetch(req))
    assert resp.status == 200
    app = _CapturingApp.last
    assert app is not None, "entry never constructed the App"
    assert isinstance(getattr(app, "ai", None), entry._AI), "fetch must wire app.ai = _AI(env) - regression ebb0a93"
    assert app.ai.env.AI is ai
