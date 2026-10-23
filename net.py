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
    from js import fetch as _js_fetch, Object as _JsObject  # type: ignore
    from pyodide.ffi import to_js as _to_js  # type: ignore
    ON_WORKERS = True
except Exception:
    ON_WORKERS = False


class Http:
    def __init__(self, timeout=30.0):
        self.timeout = timeout
        self._client = None

    async def _request(self, method, url, headers=None, params=None, json=None):
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
            r = await _js_fetch(url, _to_js(init, dict_converter=_JsObject.fromEntries))
            return Resp(int(r.status), await r.text())
        import httpx
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        r = await self._client.request(method, url, headers=headers, content=body)
        return Resp(r.status_code, r.text)

    async def get(self, url, **kw):
        return await self._request("GET", url, **kw)

    async def post(self, url, **kw):
        return await self._request("POST", url, **kw)

    async def patch(self, url, **kw):
        return await self._request("PATCH", url, **kw)

    async def delete(self, url, **kw):
        return await self._request("DELETE", url, **kw)
