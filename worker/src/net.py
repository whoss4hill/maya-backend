"""Async HTTP adapter so the same backend code runs on Render (httpx) and on Cloudflare Python Workers.

On Workers the platform fetch() is used (js.fetch); elsewhere httpx.AsyncClient. Both return Resp.
Only what main.py needs: get/post/patch/delete with headers, params and a JSON body.
"""
import json as _json
from urllib.parse import urlencode


class Resp:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text

    def json(self):
        return _json.loads(self.text)


try:  # Cloudflare Python Workers (Pyodide) only
    from js import fetch as _js_fetch, Object as _JsObject, FormData as _FormData, Blob as _Blob, Uint8Array as _Uint8Array  # type: ignore
    from pyodide.ffi import to_js as _to_js  # type: ignore
    ON_WORKERS = True
except Exception:
    ON_WORKERS = False


class Http:
    def __init__(self, timeout=30.0):
        self.timeout = timeout
        self._client = None

    async def _request(self, method, url, headers=None, params=None, json=None, timeout=None):
        if params:
            url = url + ("&" if "?" in url else "?") + urlencode(params)
        headers = dict(headers or {})
        body = None
        if json is not None:
            body = _json.dumps(json)
            headers.setdefault("Content-Type", "application/json")
        if ON_WORKERS:
            init = {"method": method, "headers": headers}
            if body is not None:
                init["body"] = body
            import asyncio as _aio

            async def _go():
                r = await _js_fetch(url, _to_js(init, dict_converter=_JsObject.fromEntries))
                return Resp(int(r.status), await r.text())
            return await _aio.wait_for(_go(), timeout or self.timeout)  # Workers fetch has no timeout of its own
        import httpx
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        r = await self._client.request(method, url, headers=headers, content=body)
        return Resp(r.status_code, r.text)

    async def post_file(self, url, fields, filename, data, mime="application/pdf", field="document"):
        """multipart/form-data POST with one file (Telegram sendDocument)."""
        if ON_WORKERS:
            fd = _FormData.new()
            for k, v in fields.items():
                fd.append(k, str(v))
            u8 = _Uint8Array.new(_to_js(list(data)))
            blob = _Blob.new(_to_js([u8]), _to_js({"type": mime}, dict_converter=_JsObject.fromEntries))
            fd.append(field, blob, filename)
            r = await _js_fetch(url, _to_js({"method": "POST", "body": fd}, dict_converter=_JsObject.fromEntries))
            return Resp(int(r.status), await r.text())
        import httpx
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        r = await self._client.post(url, data={k: str(v) for k, v in fields.items()}, files={field: (filename, data, mime)})
        return Resp(r.status_code, r.text)

    async def get_bytes(self, url, timeout=None):
        """Raw binary GET (e.g. a Telegram file download). Returns (status, bytes)."""
        if ON_WORKERS:
            import asyncio as _aio

            async def _go():
                r = await _js_fetch(url)
                buf = await r.arrayBuffer()
                return int(r.status), bytes(_Uint8Array.new(buf).to_py())
            return await _aio.wait_for(_go(), timeout or self.timeout)
        import httpx
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        r = await self._client.get(url)
        return r.status_code, r.content

    async def post_bytes(self, url, headers=None, json=None, timeout=None):
        """JSON POST with a raw binary response (e.g. ElevenLabs TTS). Returns (status, bytes)."""
        headers = dict(headers or {})
        body = _json.dumps(json) if json is not None else None
        if body is not None:
            headers.setdefault("Content-Type", "application/json")
        if ON_WORKERS:
            init = {"method": "POST", "headers": headers}
            if body is not None:
                init["body"] = body
            import asyncio as _aio

            async def _go():
                r = await _js_fetch(url, _to_js(init, dict_converter=_JsObject.fromEntries))
                buf = await r.arrayBuffer()
                return int(r.status), bytes(_Uint8Array.new(buf).to_py())
            return await _aio.wait_for(_go(), timeout or self.timeout)
        import httpx
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        r = await self._client.post(url, headers=headers, content=body)
        return r.status_code, r.content

    async def get(self, url, **kw):
        return await self._request("GET", url, **kw)

    async def put(self, url, **kw):
        return await self._request("PUT", url, **kw)

    async def post(self, url, **kw):
        return await self._request("POST", url, **kw)

    async def patch(self, url, **kw):
        return await self._request("PATCH", url, **kw)

    async def delete(self, url, **kw):
        return await self._request("DELETE", url, **kw)
