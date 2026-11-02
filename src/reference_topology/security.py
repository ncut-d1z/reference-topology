"""Loopback app session, same-origin writes, and bounded request bodies."""
from __future__ import annotations

import hmac
from http.cookies import SimpleCookie
import secrets
from urllib.parse import urlsplit

from starlette.responses import JSONResponse


class BodyTooLarge(Exception):
    pass


class LocalAccessMiddleware:
    def __init__(self, app, settings, session_id, csrf_token):
        self.app, self.settings = app, settings
        self.session_id, self.csrf_token = session_id, csrf_token

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        host = headers.get(b"host", b"").decode("latin1")
        try:
            hostname = urlsplit("//" + host).hostname
        except ValueError:
            hostname = None
        allowed = {"127.0.0.1", "localhost", "::1", *self.settings.extra_hosts}
        path, method = scope["path"], scope["method"]
        async def reject(code, message, status=403):
            response = JSONResponse({"error": {"code": code, "message": message}}, status_code=status)
            await response(scope, receive, send)
        if hostname not in allowed:
            return await reject("invalid_host", "拒绝未知 Host")
        origin = headers.get(b"origin", b"").decode("latin1")
        expected_origins = {f"{scope.get('scheme', 'http')}://{host}"}
        if origin and origin not in expected_origins:
            return await reject("cross_origin", "只允许工作台同源请求")
        if headers.get(b"sec-fetch-site") == b"cross-site" and not (path == "/" and method == "GET"):
            return await reject("cross_origin", "不允许其他网站访问本机工作台")
        protected = path.startswith("/api/") and path not in {"/api/session", "/api/health"}
        cookies = SimpleCookie()
        try:
            cookies.load(headers.get(b"cookie", b"").decode("latin1"))
        except Exception:
            pass
        value = cookies.get("rt_session")
        if protected and (not value or not hmac.compare_digest(value.value, self.session_id)):
            return await reject("session_required", "请从工作台首页重新连接", 401)
        if method not in {"GET", "HEAD", "OPTIONS"}:
            token = headers.get(b"x-csrf-token", b"").decode("latin1")
            if not hmac.compare_digest(token, self.csrf_token):
                return await reject("csrf_required", "请求令牌失效，请刷新工作台")
        limit = self.settings.max_pdf_size_mb * 1024 * 1024 + 1024 * 1024 if path == "/api/files/upload" else 2 * 1024 * 1024
        try:
            length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            return await reject("invalid_length", "无效请求长度", 400)
        if length < 0 or length > limit:
            return await reject("request_too_large", "本次请求超过大小限制，请分批导入", 413)
        consumed = 0
        started = False
        async def bounded_receive():
            nonlocal consumed
            message = await receive()
            if message["type"] == "http.request":
                consumed += len(message.get("body", b""))
                if consumed > limit:
                    raise BodyTooLarge()
            return message
        async def guarded_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                additions = [(b"x-content-type-options", b"nosniff"), (b"x-frame-options", b"SAMEORIGIN"),
                             (b"cross-origin-resource-policy", b"same-origin"), (b"referrer-policy", b"same-origin")]
                if path == "/" or path.startswith("/api/"):
                    additions.append((b"cache-control", b"no-store"))
                if path == "/":
                    additions.append((b"content-security-policy", b"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'self'"))
                message = {**message, "headers": list(message.get("headers", [])) + additions}
            await send(message)
        try:
            await self.app(scope, bounded_receive, guarded_send)
        except BodyTooLarge:
            if not started:
                await reject("request_too_large", "本次请求超过大小限制", 413)
