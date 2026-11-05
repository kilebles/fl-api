"""End-to-end tests for the MCP Streamable HTTP & standalone control plane."""

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
import pytest

from src.accounts.repository import AccountRepository
from src.api.app import create_app
from src.core.settings import Settings
from src.dolphin import cooldowns
from src.engine import scheduler
from src.jobs.runner import JobRunner
from tests.fakes import FakeSession
from tests.test_prompts import FakeLLM


@pytest.fixture(autouse=True)
def fast_scheduler(monkeypatch):
    monkeypatch.setattr(scheduler, "PICK_POLL_INTERVAL", 0.02)
    monkeypatch.setattr(scheduler, "INTER_GENERATION_PAUSE", (0.0, 0.0))


def _make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    defaults: dict[str, Any] = {
        "_env_file": None,
        "api_token": "test-mcp-secret",
        "auth_required": True,
        "database_url": f"sqlite+aiosqlite:///{tmp_path / 'mcp.sqlite'}",
        "work_dir": tmp_path / "work",
        "storage_dir": tmp_path / "storage",
        "accounts_dir": tmp_path / "accounts",
        "logs_dir": tmp_path / "logs",
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _call_result_json(result) -> Any:
    assert not result.is_error, f"MCP tool returned error: {result.content}"
    if result.structured_content is not None:
        sc = result.structured_content
        if isinstance(sc, dict) and set(sc.keys()) == {"result"}:
            return sc["result"]
        return sc
    text = result.content[0].text
    return json.loads(text)


async def test_mcp_requires_auth_and_restarts_cleanly(tmp_path: Path):
    settings = _make_settings(tmp_path)
    app = create_app(settings)

    for _ in range(2):
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000") as http:
                unauth = await http.post("/api/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
                assert unauth.status_code == 401

                authed_http = httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://localhost:8000",
                    headers={"Authorization": "Bearer test-mcp-secret"},
                )
                async with authed_http:
                    async with streamable_http_client("http://localhost:8000/api/mcp", http_client=authed_http) as (
                        read_stream,
                        write_stream,
                    ):
                        async with ClientSession(read_stream, write_stream) as session:
                            await session.initialize()
                            tools = await session.list_tools()
                            tool_names = {t.name for t in tools.tools}
                            assert {
                                "get_system_overview",
                                "get_accounts_status",
                                "diagnose_job",
                                "submit_generation_job",
                                "handoff_prompts_to_job",
                                "start_infographics_run",
                                "analyze_avatar_reference",
                                "order_avatar_references",
                                "generate_avatar_angles",
                                "cancel_avatar_angles",
                            } <= tool_names

                            resources = await session.list_resources()
                            assert {"fl://modes", "fl://pipeline"} <= {
                                str(r.uri) for r in resources.resources
                            }
                            templates = await session.list_resource_templates()
                            assert "fl://jobs/{job_id}" in {
                                str(t.uri_template) for t in templates.resource_templates
                            }

                            prompts = await session.list_prompts()
                            assert {"ops_health_check", "launch_video_pipeline", "troubleshoot_job"} <= {
                                p.name for p in prompts.prompts
                            }


async def test_mcp_monitoring_cooldowns_proxies_and_logs(tmp_path: Path):
    settings = _make_settings(tmp_path)
    repo = AccountRepository(settings.accounts_dir)
    acc = repo.create("dolphin-999")
    repo.add_proxies(acc.name, ["10.0.0.1:8080:user:pass", "10.0.0.2:8080:user:pass"])
    (acc.directory / "dead_proxies.txt").write_text("10.0.0.1:8080\n")

    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    (settings.logs_dir / "2026-11-05_01-00-00.log").write_text(
        "2026-11-05 01:00:01.000 | WARNING  | engine:run:10 - account_1 quota Bearer super-secret ya29.tok123\n"
        "2026-11-05 01:00:02.000 | ERROR    | jobs:run:20 - job_abc123 failed on http://u:p@10.0.0.1:8080\n",
        encoding="utf-8",
    )

    cooldowns.mark_cooldown("dolphin-999", "account_1")
    cooldowns.mark_image_cooldown("dolphin-999", "account_1")
    cooldowns.mark_video_denied("dolphin-999", "account_1")

    try:
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            # Also verify `?token=` query-string authentication works end-to-end
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://localhost:8000",
            ) as http:
                async with streamable_http_client(
                    "http://localhost:8000/api/mcp?token=test-mcp-secret",
                    http_client=http,
                ) as (read_stream, write_stream):
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()

                        acc_status = _call_result_json(await session.call_tool("get_accounts_status", {}))
                        assert acc_status["count"] == 1
                        a0 = acc_status["accounts"][0]
                        assert a0["name"] == "account_1"
                        assert a0["cooldown"] > 0 and a0["image_pause"] > 0 and a0["video_pause"] > 0
                        assert a0["dead_proxy_list"] == ["10.0.0.1:8080"]

                        after_reset = _call_result_json(
                            await session.call_tool(
                                "reset_account_cooldown",
                                {"account_name": "account_1", "kind": "all"},
                            )
                        )
                        assert after_reset["cooldown"] == 0 and after_reset["image_pause"] == 0 and after_reset["video_pause"] == 0

                        after_revive = _call_result_json(
                            await session.call_tool(
                                "revive_account_proxies",
                                {"account_name": "account_1"},
                            )
                        )
                        assert after_revive["dead_proxies"] == 0
                        assert after_revive["dead_proxy_list"] == []

                        logs = _call_result_json(
                            await session.call_tool("tail_server_logs", {"lines": 10})
                        )
                        assert logs["total_matched"] == 2
                        joined = "\n".join(logs["lines"])
                        assert "super-secret" not in joined
                        assert "ya29.tok123" not in joined
                        assert "u:p@" not in joined
                        assert "[REDACTED]" in joined
    finally:
        cooldowns.reset_cooldown("dolphin-999")
        cooldowns.reset_image_cooldown("dolphin-999")
        cooldowns.reset_video_denied("dolphin-999")


async def test_mcp_full_pipeline_project_avatar_prompts_handoff_and_job(tmp_path: Path, monkeypatch):
    settings = _make_settings(tmp_path)
    repo = AccountRepository(settings.accounts_dir)
    repo.create("1")
    filtered_vid = f"{'video prompt number 1 ' * 3}".strip() + ", 35mm grain"
    fake_session = FakeSession("account_1", tmp_path / "scratch", filtered={filtered_vid})
    runner = JobRunner(settings, repo)
    monkeypatch.setattr(runner, "_make_sessions", lambda accounts, strategy: [fake_session])

    app = create_app(settings, runner=runner, llm=FakeLLM())
    avatar_pic = tmp_path / "caesar.png"
    avatar_pic.write_bytes(b"\x89PNG\r\n\x1a\nfake")

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost:8000",
            headers={"Authorization": "Bearer test-mcp-secret"},
        ) as http:
            async with streamable_http_client("http://localhost:8000/api/mcp", http_client=http) as (
                read_stream,
                write_stream,
            ):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()

                    proj = _call_result_json(
                        await session.call_tool(
                            "save_project",
                            {"title": "Римская Империя", "style": "35mm grain"},
                        )
                    )
                    avatar = _call_result_json(
                        await session.call_tool(
                            "save_avatar",
                            {
                                "name": "Цезарь",
                                "appearance": "purple toga, laurel wreath",
                                "local_image_paths": [str(avatar_pic)],
                            },
                        )
                    )
                    tpl = _call_result_json(
                        await session.call_tool(
                            "create_prompt_template",
                            {"brief_text": "Wide cinematic shots of Rome", "title": "Rome Style"},
                        )
                    )
                    run = _call_result_json(
                        await session.call_tool(
                            "start_prompt_generation",
                            {
                                "backend": "builtin",
                                "template_id": tpl["id"],
                                "scenario_text": "The legion marched in 1100.\n\nWinter came early.",
                                "pair_count": 2,
                                "project_id": proj["id"],
                            },
                        )
                    )
                    for _ in range(80):
                        run_info = _call_result_json(
                            await session.call_tool(
                                "get_prompt_run_details",
                                {"run_id": run["id"], "backend": "builtin"},
                            )
                        )
                        if run_info["run"]["state"] in ("done", "failed"):
                            break
                        await asyncio.sleep(0.02)
                    assert run_info["run"]["state"] == "done"

                    job = _call_result_json(
                        await session.call_tool(
                            "handoff_prompts_to_job",
                            {
                                "prompt_run_id": run["id"],
                                "backend": "builtin",
                                "mode": "i2i2v",
                                "avatar_id": avatar["id"],
                            },
                        )
                    )

                    for _ in range(80):
                        details = _call_result_json(
                            await session.call_tool("get_job_details", {"job_id": job["id"]})
                        )
                        if details["job"]["state"] in ("done", "stopped", "failed"):
                            break
                        await asyncio.sleep(0.03)

                    assert details["job"]["state"] == "done"
                    assert details["job"]["progress"]["videos_done"] == 4
                    row_1 = next(r for r in details["rows"] if r["name"] == "1.1")
                    assert "purple toga, laurel wreath" in row_1["image_prompt"]
                    assert row_1["image_prompt"].endswith(", 35mm grain")
                    assert row_1["prompt"].endswith(", 35mm grain")

                    # Also test submit_generation_job with duration + aspect_ratio + list_generation_modes + analyze_avatar_reference
                    modes_catalog = _call_result_json(await session.call_tool("list_generation_modes", {}))
                    t2v_meta = next(m for m in modes_catalog if m["id"] == "t2v")
                    assert t2v_meta["durations"] == [4, 6, 8] and t2v_meta["default_duration"] == 8
                    flash_meta = next(m for m in modes_catalog if m["id"] == "flash_t2v")
                    assert flash_meta["durations"] == [4, 6, 8, 10] and flash_meta["default_duration"] == 10

                    ref_analysis = _call_result_json(
                        await session.call_tool("analyze_avatar_reference", {"file_path": str(avatar_pic)})
                    )
                    assert "recommended_angles" in ref_analysis and ref_analysis["key"].startswith("inputs/")

                    ordered_refs = _call_result_json(
                        await session.call_tool("order_avatar_references", {"image_keys": [ref_analysis["key"]]})
                    )
                    assert ordered_refs["keys"] == [ref_analysis["key"]]

                    custom_job = _call_result_json(
                        await session.call_tool(
                            "submit_generation_job",
                            {
                                "mode": "t2v",
                                "duration": 4,
                                "aspect_ratio": "9:16",
                                "prompts": ["good shot", "video prompt number 1 video prompt number 1 video prompt number 1"],
                                "project_id": proj["id"],
                            },
                        )
                    )
                    assert custom_job["duration"] == 4 and custom_job["aspect_ratio"] == "9:16"
                    for _ in range(80):
                        c_details = _call_result_json(
                            await session.call_tool("get_job_details", {"job_id": custom_job["id"]})
                        )
                        if c_details["job"]["state"] in ("done", "stopped", "failed"):
                            break
                        await asyncio.sleep(0.03)
                    assert "veo_3_1_t2v_lite_4s_low_priority" in fake_session.model_keys
                    assert "9:16" in fake_session.video_aspect_ratios

                    diag = _call_result_json(
                        await session.call_tool("diagnose_job", {"job_id": custom_job["id"]})
                    )
                    assert diag["skipped_count"] == 1
                    assert diag["skipped_rows"][0]["row"] == "2"

                    job_res = await session.read_resource(f"fl://jobs/{job['id']}")
                    assert json.loads(job_res.contents[0].text)["id"] == job["id"]


async def test_mcp_respects_non_admin_user_isolation(tmp_path: Path):
    settings = _make_settings(tmp_path, first_user_admin=False)
    app = create_app(settings)

    async with app.router.lifespan_context(app):
        auth = app.state.auth
        alice, _, alice_token = await auth.login_or_register_google(
            google_sub="sub-alice",
            email="alice@example.com",
            email_verified=True,
            name="Alice",
            picture=None,
        )
        bob, _, bob_token = await auth.login_or_register_google(
            google_sub="sub-bob",
            email="bob@example.com",
            email_verified=True,
            name="Bob",
            picture=None,
        )
        assert alice.role == "user" and bob.role == "user"

        # Alice creates a project via MCP
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost:8000",
            headers={"Authorization": f"Bearer {alice_token}"},
        ) as alice_http:
            async with streamable_http_client("http://localhost:8000/api/mcp", http_client=alice_http) as (rs, ws):
                async with ClientSession(rs, ws) as alice_mcp:
                    await alice_mcp.initialize()
                    alice_proj = _call_result_json(
                        await alice_mcp.call_tool("save_project", {"title": "Alice Secret Video"})
                    )
                    assert alice_proj["title"] == "Alice Secret Video"

                    # Non-admin cannot view Dolphin accounts
                    acc_res = await alice_mcp.call_tool("get_accounts_status", {})
                    assert acc_res.is_error

        # Bob cannot see Alice's project via MCP
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost:8000",
            headers={"Authorization": f"Bearer {bob_token}"},
        ) as bob_http:
            async with streamable_http_client("http://localhost:8000/api/mcp", http_client=bob_http) as (rs, ws):
                async with ClientSession(rs, ws) as bob_mcp:
                    await bob_mcp.initialize()
                    bob_projects = _call_result_json(await bob_mcp.call_tool("list_projects", {}))
                    assert bob_projects == []


async def test_stdio_mcp_script_handshake():
    import sys

    script = Path(__file__).resolve().parent.parent / "scripts" / "mcp_server.py"
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(script), "--base-url", "http://127.0.0.1:8000", "--token", "dummy"],
    )
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            init = await session.initialize()
            assert init.server_info.name == "fl-platform"
            tools = await session.list_tools()
            assert len(tools.tools) >= 25
            modes_res = await session.read_resource("fl://modes")
            modes_data = json.loads(modes_res.contents[0].text)
            assert any(m["id"] == "i2i2v" for m in modes_data)


async def test_personal_mcp_tokens_and_gateway_verify(tmp_path: Path):
    settings = _make_settings(tmp_path, first_user_admin=False)
    app = create_app(settings)

    async with app.router.lifespan_context(app):
        auth = app.state.auth
        alice, _, alice_sess = await auth.login_or_register_google(
            google_sub="sub-alice-tok",
            email="alice-tok@example.com",
            email_verified=True,
            name="Alice",
            picture=None,
        )
        assert alice.role == "user"

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost:8000",
            headers={"Authorization": f"Bearer {alice_sess}"},
        ) as user_http:
            created = await user_http.post("/api/auth/tokens", json={"name": "Antigravity IDE"})
            assert created.status_code == 201
            payload = created.json()
            raw_key = payload["token"]
            token_id = payload["id"]
            assert raw_key.startswith("flk_")
            assert payload["token_prefix"] == raw_key[:12]

            listed = await user_http.get("/api/auth/tokens")
            assert listed.status_code == 200
            items = listed.json()
            assert len(items) == 1
            assert items[0]["id"] == token_id
            assert "token" not in items[0]

            # Caddy forward_auth verification works with flk_...
            verify = await user_http.get("/api/auth/verify", headers={"Authorization": f"Bearer {raw_key}"})
            assert verify.status_code == 200
            assert "X-Fl-Gateway-Auth" in verify.headers

        # Connect over Streamable HTTP using ?token=flk_... query auth (for headerless MCP clients)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost:8000",
        ) as mcp_http:
            async with streamable_http_client(
                f"http://localhost:8000/api/mcp?token={raw_key}",
                http_client=mcp_http,
            ) as (rs, ws):
                async with ClientSession(rs, ws) as mcp_session:
                    await mcp_session.initialize()
                    proj = _call_result_json(
                        await mcp_session.call_tool("save_project", {"title": "Token Project", "style": "noir"})
                    )
                    assert proj["title"] == "Token Project"

        # Revoke the token and verify it is immediately rejected
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost:8000",
            headers={"Authorization": f"Bearer {alice_sess}"},
        ) as user_http:
            revoked = await user_http.delete(f"/api/auth/tokens/{token_id}")
            assert revoked.status_code == 204

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost:8000",
        ) as unauth_http:
            rejected = await unauth_http.post(
                "/api/mcp",
                headers={"Authorization": f"Bearer {raw_key}"},
                json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            )
            assert rejected.status_code == 401
