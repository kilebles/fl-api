"""MCP server factory, resources, prompts, and FastAPI Streamable HTTP mounting."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import json
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from src.api.auth import get_principal
from src.core.settings import Settings
from src.mcp.client import FlPlatformClient, extract_caller_headers
from src.mcp.tools_jobs import build_modes_catalog, register_job_tools
from src.mcp.tools_monitor import register_monitor_tools
from src.mcp.tools_pipeline import register_pipeline_tools

SERVER_INSTRUCTIONS = """
Control plane for the Motion Pr / Google Flow video production platform.

Architecture & Workflow Stages:
1. Monitoring & Accounts:
   - Call `get_system_overview` first to inspect JobQueue, browser accounts, infographics, and upstream services.
   - Use `get_accounts_status`, `diagnose_job`, and `tail_server_logs` when troubleshooting waiting/stopped/failed jobs.
   - Jobs run one at a time in `JobQueue` because they share Dolphin Anty / Octo Browser profiles (pooled via `MAX_ACTIVE_BROWSERS`).

2. Projects («Ролики») & 360° Avatars:
   - Projects hold a `title` and a `style` suffix automatically appended to prompts (`save_project`, `list_projects`).
   - Avatars hold ordered multi-angle reference images and appearance text for `i2i2v` / `img` character consistency (`list_avatars`, `save_avatar`, `analyze_avatar_reference`, `order_avatar_references`, `generate_avatar_angles`, `cancel_avatar_angles`).

3. Video Durations & Aspect Ratios:
   - Veo 3.1 modes (`t2v`, `i2v`, `i2i2v`): `duration` supports `4`, `6`, or `8` seconds (default `8`); `aspect_ratio` supports `"16:9"` or `"9:16"`.
   - Omni 1.1 Flash modes (`flash_t2v`, `flash_i2v`): `duration` supports `4`, `6`, `8`, or `10` seconds (default `10`); `aspect_ratio` supports `"16:9"` or `"9:16"`.
   - Chained clips (`f2v`): `duration` supports `4`, `6`, `8` (Veo 3.1), or `10` seconds (Omni 1.1 Flash), default `8`; `aspect_ratio` supports `"16:9"` or `"9:16"`.
   - Image-only (`img`): `aspect_ratio` supports `"16:9"`, `"9:16"`, or `"1:1"`.

4. End-to-End Video Production Stages:
   - Stage 1 (Scenario & Analytics): `fl-scenario` (`list_scenario_projects`, `create_scenario_project`, `control_scenario_project`).
   - Stage 2 (Prompts): `fl-prompts` or built-in `fl-api` (`start_prompt_generation`, `get_prompt_run_details`).
   - Stage 3 (Veo / Image Generation): Use `handoff_prompts_to_job` to send finished prompt pairs directly into `JobQueue`, or `submit_generation_job` for custom/Excel/single runs (pass `duration` and `aspect_ratio` as needed).
   - Stage 4 (Infographics): `start_infographics_run` discovers people/places/artifacts via Wikidata/Wikimedia and renders Remotion cards.
   - Stage 5 (Voice & Footage): `start_voice_job` (translation + Fish TTS + mixing) and `start_footage_project`.
""".strip()


def _build_transport_security(settings: Settings) -> TransportSecuritySettings:
    allowed_hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*", "testserver", "testserver:*", "fl-internal.local"]
    allowed_origins = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*", "http://testserver"]

    for raw_url in (
        settings.public_url,
        settings.frontend_url,
        settings.mcp_gateway_url,
        *settings.cors_origins,
    ):
        if not raw_url:
            continue
        parsed = urlparse(raw_url)
        if parsed.hostname:
            host = parsed.hostname.lower()
            for entry in (host, f"{host}:*"):
                if entry not in allowed_hosts:
                    allowed_hosts.append(entry)
        if parsed.scheme and parsed.netloc:
            origin = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
            if origin not in allowed_origins:
                allowed_origins.append(origin)

    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )


class _AuthenticatedMCPASGIApp:
    """Enforce `fl-api` authentication (`API_TOKEN`, session cookie, or gateway token) before MCP transport."""

    def __init__(self, app: FastAPI) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        inner: ASGIApp | None = getattr(self._app.state, "mcp_http_endpoint", None)
        if inner is None:
            resp = JSONResponse({"detail": "MCP session manager is not running"}, status_code=503)
            await resp(scope, receive, send)
            return
        if scope["type"] != "http":
            await inner(scope, receive, send)
            return
        request = Request(scope)
        try:
            await get_principal(request)
        except HTTPException as exc:
            resp = JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
            await resp(scope, receive, send)
            return
        await inner(scope, receive, send)


def create_mcp_server(
    settings: Settings,
    *,
    asgi_app: ASGIApp | None = None,
    base_url: str | None = None,
    api_token: str | None = None,
) -> MCPServer:
    """Create a configured `MCPServer` with all monitoring, queue, and pipeline tools, resources, and prompts."""
    client = FlPlatformClient(
        settings,
        asgi_app=asgi_app,
        base_url=base_url,
        api_token=api_token,
    )
    mcp = MCPServer(
        name="fl-platform",
        title="Motion Pr / Flow Control Plane",
        description="Monitoring, queues, and multi-stage AI video generation control plane",
        instructions=SERVER_INSTRUCTIONS,
        version="1.0.0",
    )

    register_monitor_tools(mcp, client)
    register_job_tools(mcp, client)
    register_pipeline_tools(mcp, client)

    # ------------------------------------------------------------------ MCP Resources

    @mcp.resource(
        "fl://modes",
        name="generation_modes",
        title="Veo & Image Generation Modes",
        description="All supported generation modes (t2v, flash_t2v, img, i2v, flash_i2v, i2i2v, f2v), supported video durations, aspect ratios, and input specs.",
        mime_type="application/json",
    )
    def modes_resource() -> str:
        return json.dumps(build_modes_catalog(), ensure_ascii=False, indent=2)

    @mcp.resource(
        "fl://pipeline",
        name="pipeline_reference",
        title="Platform Pipeline Reference",
        description="Reference guide to Motion Pr production stages, queues, strategies, and recovery behavior.",
        mime_type="application/json",
    )
    def pipeline_resource() -> str:
        guide = {
            "stages": [
                {"stage": 1, "name": "scenario", "service": "fl-scenario", "flow": "analytics -> brief -> plan -> chapters -> done"},
                {"stage": 2, "name": "prompts", "service": "fl-prompts / fl-api", "flow": "brief/template -> (img, vid) pairs per paragraph"},
                {"stage": 3, "name": "generation", "service": "fl-api JobQueue", "flow": "queued -> running/waiting -> done (Veo + Nano Banana)"},
                {"stage": 4, "name": "infographics", "service": "fl-api InfographicsService", "flow": "searching (Wikidata/Wikimedia) -> rendering (Remotion) -> done"},
                {"stage": 5, "name": "voice", "service": "fl-voice_translator", "flow": "translate -> voice (Fish S2.1 / Drama 3) -> mix"},
                {"stage": 6, "name": "footage", "service": "fl-footage", "flow": "plan -> search -> extra -> cut -> done"},
            ],
            "job_states": ["queued", "running", "waiting", "done", "stopped", "failed", "cancelled"],
            "strategies": {
                "parallel": "One worker per enabled browser account concurrently (capped by MAX_ACTIVE_BROWSERS).",
                "sequential": "Single session rotating across accounts; mandatory for chained f2v mode.",
            },
        }
        return json.dumps(guide, ensure_ascii=False, indent=2)

    @mcp.resource(
        "fl://jobs/{job_id}",
        name="job_snapshot",
        title="Job Snapshot by ID",
        description="Read a specific generation job's status and queue position (scoped to the authenticated caller).",
        mime_type="application/json",
    )
    async def job_resource(job_id: str, ctx: Context) -> str:
        detail = await client.request("GET", f"/api/jobs/{job_id}", caller_headers=extract_caller_headers(ctx))
        return json.dumps(detail, ensure_ascii=False, indent=2, default=str)

    # ------------------------------------------------------------------ MCP Prompts

    @mcp.prompt(
        name="ops_health_check",
        title="Operations & Queue Audit",
        description="Audit active queues, stopped/waiting jobs, browser account cooldowns, and dead proxies.",
    )
    def ops_health_check() -> str:
        return (
            "Выполни полный аудит платформы:\n"
            "1. Вызови `get_system_overview` и `get_accounts_status`.\n"
            "2. Если есть джобы в статусе `waiting`, `stopped` или `failed`, вызови для них `diagnose_job`.\n"
            "3. Дай краткую сводку на русском языке: что сейчас генерируется, есть ли узкие места (прокси, квоты изображений/видео, кулдауны) и какие действия рекомендуешь."
        )

    @mcp.prompt(
        name="launch_video_pipeline",
        title="Launch Multi-Stage Video Pipeline",
        description="Coordinate creating a project, generating prompts/infographics/voice, and queuing Veo generation.",
    )
    def launch_video_pipeline(topic_or_title: str, style: str = "", mode: str = "i2v") -> str:
        return (
            f"Запусти производство ролика «{topic_or_title}» (режим генерации: `{mode}`, стиль: `{style or 'по умолчанию'}`):\n"
            "1. Проверь готовность очередей и аккаунтов через `get_system_overview`.\n"
            "2. Проверь существующие проекты через `list_projects` или создай новый через `save_project`.\n"
            "3. Согласуй со мной источник сценария (существующий проект в `fl-scenario`, локальный файл или текст), после чего запусти этапы (промпты, инфографику, озвучку) и передай готовые промпты в `handoff_prompts_to_job`."
        )

    @mcp.prompt(
        name="troubleshoot_job",
        title="Troubleshoot Generation Job",
        description="Deep-dive root-cause analysis for a specific job ID.",
    )
    def troubleshoot_job(job_id: str) -> str:
        return (
            f"Проведи диагностику джобы `{job_id}`:\n"
            f"1. Вызови `diagnose_job(job_id='{job_id}')`.\n"
            "2. Объясни на русском языке: сколько строк готово, почему строки попали в skip (если есть), не упёрлись ли аккаунты в квоту или мёртвые прокси, и предложи точное решение (например, `revive_account_proxies`, `reset_account_cooldown` или `control_job(action='retry')`)."
        )

    return mcp


def mount_mcp_on_app(app: FastAPI, settings: Settings) -> MCPServer:
    """Attach authenticated Streamable HTTP MCP routes (`/api/mcp` and `/mcp`) on `app`."""
    mcp = create_mcp_server(settings, asgi_app=app)
    authed_endpoint = _AuthenticatedMCPASGIApp(app)
    for route_path in ("/api/mcp", "/api/mcp/", "/mcp", "/mcp/"):
        app.router.routes.append(Route(route_path, endpoint=authed_endpoint))
    app.state.mcp = mcp
    return mcp


@asynccontextmanager
async def mcp_lifespan(app: FastAPI, settings: Settings) -> AsyncIterator[None]:
    """Create a fresh Streamable HTTP session manager on each FastAPI lifespan cycle."""
    mcp: MCPServer | None = getattr(app.state, "mcp", None)
    if mcp is None or not settings.mcp_enabled:
        yield
        return
    sub_app = mcp.streamable_http_app(
        streamable_http_path="/api/mcp",
        transport_security=_build_transport_security(settings),
    )
    app.state.mcp_http_endpoint = sub_app.routes[0].endpoint
    async with mcp.session_manager.run():
        try:
            yield
        finally:
            app.state.mcp_http_endpoint = None
