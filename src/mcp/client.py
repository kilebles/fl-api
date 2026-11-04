"""Unified async client for fl-api and optional upstream pipeline services."""

from __future__ import annotations

import io
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

import docx
import httpx
from starlette.types import ASGIApp

from src.core.settings import Settings

FORWARDED_HEADERS = ("authorization", "cookie", "x-fl-gateway-auth")


class PlatformAPIError(RuntimeError):
    """Structured error returned by one of the platform HTTP services."""

    def __init__(self, status_code: int, detail: str, service: str = "fl-api", path: str = "") -> None:
        self.status_code = status_code
        self.detail = detail
        self.service = service
        self.path = path
        super().__init__(f"[{service}] HTTP {status_code} on {path}: {detail}")


def docx_bytes_from_text(text_or_paragraphs: str | list[str]) -> bytes:
    """Build a clean .docx document in memory from plain text or paragraph list."""
    doc = docx.Document()
    if isinstance(text_or_paragraphs, str):
        blocks = [p.strip() for p in text_or_paragraphs.replace("\r\n", "\n").split("\n\n") if p.strip()]
        if len(blocks) <= 1:
            blocks = [p.strip() for p in text_or_paragraphs.splitlines() if p.strip()]
    else:
        blocks = [str(p).strip() for p in text_or_paragraphs if str(p).strip()]
    for block in blocks:
        doc.add_paragraph(block)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def with_style(text: str | None, style: str | None) -> str | None:
    """Append a project's style suffix to a prompt, matching fl-frontend's `withStyle`."""
    if not text:
        return text
    clean_style = (style or "").strip()
    return f"{text}, {clean_style}" if clean_style else text


def extract_caller_headers(ctx: Any) -> Mapping[str, str] | None:
    """Extract HTTP auth headers (and `?token=` fallback) from an MCP `Context` when running over HTTP."""
    if ctx is None:
        return None
    raw = getattr(ctx, "headers", None)
    out: dict[str, str] = {k.lower(): v for k, v in raw.items()} if raw else {}
    if "authorization" not in out:
        try:
            req = ctx.request_context.request
            token = req.query_params.get("token", "").strip() if req is not None and hasattr(req, "query_params") else ""
            if token:
                out["authorization"] = f"Bearer {token}"
        except Exception:
            pass
    return out or None


def _infer_gateway_url(settings: Settings) -> str | None:
    """Resolve explicit `mcp_gateway_url` or fall back to non-loopback `frontend_url` behind Caddy."""
    if settings.mcp_gateway_url:
        return settings.mcp_gateway_url.rstrip("/")
    if settings.frontend_url:
        parsed = urlparse(settings.frontend_url)
        if parsed.scheme and parsed.hostname and parsed.hostname.lower() not in ("localhost", "127.0.0.1", "::1", "testserver"):
            return settings.frontend_url.rstrip("/")
    return None


class FlPlatformClient:
    """
    Async client over the Motion Pr / Flow platform.

    In-process mode (`asgi_app` provided): calls to core `fl-api` routes execute directly
    against the FastAPI ASGI app while forwarding caller auth headers from MCP `Context`.
    Standalone mode (`asgi_app=None`): all calls go over HTTP to `base_url` (or per-service
    URL overrides) with `Bearer <api_token>`.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        asgi_app: ASGIApp | None = None,
        base_url: str | None = None,
        api_token: str | None = None,
    ) -> None:
        self.settings = settings
        self._asgi_app = asgi_app
        self.base_url = (base_url or settings.mcp_gateway_url or settings.public_url or "http://127.0.0.1:8000").rstrip("/")
        self.api_token = api_token if api_token is not None else settings.api_token
        self.gateway_url = _infer_gateway_url(settings)
        self.scenario_url = settings.scenario_api_url.rstrip("/") if settings.scenario_api_url else self.gateway_url
        self.prompts_url = settings.prompts_api_url.rstrip("/") if settings.prompts_api_url else self.gateway_url
        self.voice_url = settings.voice_api_url.rstrip("/") if settings.voice_api_url else self.gateway_url
        self.footage_url = settings.footage_api_url.rstrip("/") if settings.footage_api_url else self.gateway_url

    def _headers(
        self,
        caller_headers: Mapping[str, str] | None = None,
        *,
        target_origin: str | None = None,
    ) -> dict[str, str]:
        out: dict[str, str] = {"X-Requested-With": "fl-mcp"}
        has_caller_auth = False
        if caller_headers:
            lowered = {k.lower(): v for k, v in caller_headers.items()}
            for name in FORWARDED_HEADERS:
                if lowered.get(name):
                    out[name] = lowered[name]
                    has_caller_auth = True
            if "cookie" in out and "origin" not in out:
                origin = lowered.get("origin") or target_origin or self.settings.frontend_url or self.base_url
                if origin:
                    parsed = urlparse(origin)
                    if parsed.scheme and parsed.netloc:
                        out["Origin"] = f"{parsed.scheme}://{parsed.netloc}"
        if not has_caller_auth and self.api_token:
            out["Authorization"] = f"Bearer {self.api_token}"
        return out

    def _resolve_target(self, path: str) -> tuple[str, str | None, bool]:
        """Return `(service_name, remote_base_url, use_asgi)` for a request path."""
        if path.startswith("/api/scenario"):
            target = self.scenario_url or (None if self._asgi_app else self.base_url)
            return "fl-scenario", target, False
        if path.startswith("/api/voice"):
            target = self.voice_url or (None if self._asgi_app else self.base_url)
            return "fl-voice", target, False
        if path.startswith("/api/footage"):
            target = self.footage_url or (None if self._asgi_app else self.base_url)
            return "fl-footage", target, False
        if path.startswith("/api/prompts/projects") or path in (
            "/api/prompts/health",
            "/api/prompts/options",
            "/api/prompts/niches",
            "/api/prompts/scripts",
        ) or path.startswith("/api/prompts/niches/") or path.startswith("/api/prompts/templates/builtin/"):
            target = self.prompts_url or (None if self._asgi_app else self.base_url)
            return "fl-prompts", target, False
        if self.settings.mcp_gateway_url and not path.startswith(("/api/auth", "/api/projects", "/api/infographics")):
            return "fl-api", self.settings.mcp_gateway_url.rstrip("/"), False
        if self._asgi_app is not None:
            return "fl-api", "http://fl-internal.local", True
        return "fl-api", self.base_url, False

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        data: Mapping[str, Any] | None = None,
        files: Any = None,
        caller_headers: Mapping[str, str] | None = None,
        timeout: float = 60.0,
    ) -> Any:
        """Perform a JSON or multipart request and parse the response."""
        raw, _ = await self.request_raw(
            method,
            path,
            params=params,
            json_body=json_body,
            data=data,
            files=files,
            caller_headers=caller_headers,
            timeout=timeout,
            parse_json=True,
        )
        return raw

    async def request_bytes(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        caller_headers: Mapping[str, str] | None = None,
        timeout: float = 60.0,
    ) -> tuple[bytes, str]:
        """Download binary content (e.g. a scenario .docx file) and return `(bytes, content_type)`."""
        raw, content_type = await self.request_raw(
            method,
            path,
            params=params,
            caller_headers=caller_headers,
            timeout=timeout,
            parse_json=False,
        )
        return raw, content_type

    async def request_raw(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        data: Mapping[str, Any] | None = None,
        files: Any = None,
        caller_headers: Mapping[str, str] | None = None,
        timeout: float = 60.0,
        parse_json: bool = True,
    ) -> tuple[Any, str]:
        service, target_base, use_asgi = self._resolve_target(path)
        if not target_base:
            raise PlatformAPIError(
                503,
                f"{service} URL is not configured (set {service.upper().replace('-', '_')}_API_URL or MCP_GATEWAY_URL)",
                service=service,
                path=path,
            )

        clean_params = {k: v for k, v in (params or {}).items() if v is not None}
        req_headers = self._headers(caller_headers, target_origin=target_base)
        transport = httpx.ASGITransport(app=self._asgi_app) if use_asgi and self._asgi_app is not None else None

        try:
            async with httpx.AsyncClient(
                base_url=target_base,
                transport=transport,
                timeout=timeout,
                follow_redirects=True,
            ) as client:
                resp = await client.request(
                    method,
                    path,
                    params=clean_params or None,
                    json=json_body,
                    data=data,
                    files=files,
                    headers=req_headers,
                )
        except httpx.RequestError as exc:
            raise PlatformAPIError(
                503,
                f"Service unreachable ({type(exc).__name__}: {exc})",
                service=service,
                path=path,
            ) from None

        if resp.status_code >= 400:
            detail = resp.reason_phrase or f"HTTP {resp.status_code}"
            try:
                err_json = resp.json()
                if isinstance(err_json, dict) and "detail" in err_json:
                    d = err_json["detail"]
                    detail = d if isinstance(d, str) else str(d)
                else:
                    detail = str(err_json)
            except Exception:
                text = resp.text.strip()
                if text:
                    detail = text[:400]
            raise PlatformAPIError(resp.status_code, detail, service=service, path=path)

        content_type = resp.headers.get("content-type", "")
        if not parse_json:
            return resp.content, content_type
        if resp.status_code == 204 or not resp.content:
            return {"ok": True}, content_type
        return resp.json(), content_type

    async def probe_optional(
        self,
        path: str,
        *,
        caller_headers: Mapping[str, str] | None = None,
        timeout: float = 2.5,
    ) -> dict[str, Any]:
        """Probe an optional microservice health endpoint without failing the caller."""
        service, target_base, _ = self._resolve_target(path)
        if not target_base:
            return {"service": service, "state": "not_configured"}
        try:
            payload = await self.request("GET", path, caller_headers=caller_headers, timeout=timeout)
            return {"service": service, "state": "online", "detail": payload}
        except PlatformAPIError as exc:
            if exc.status_code == 404:
                return {"service": service, "state": "not_mounted", "detail": exc.detail}
            return {"service": service, "state": "offline", "status_code": exc.status_code, "error": exc.detail}
