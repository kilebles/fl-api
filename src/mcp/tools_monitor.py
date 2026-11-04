"""Monitoring, account farm health, and root-cause diagnostic MCP tools."""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import ToolAnnotations

from src.mcp.client import FlPlatformClient, PlatformAPIError, extract_caller_headers

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True)
SAFE_MUTATE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True)


def _ctx_headers(ctx: Context | None):
    return extract_caller_headers(ctx)


async def build_system_overview(client: FlPlatformClient, caller_headers=None) -> dict[str, Any]:
    """Gather a live cross-service snapshot of queues, accounts, and upstream stages."""
    health_task = client.probe_optional("/health", caller_headers=caller_headers)
    jobs_task = client.request("GET", "/api/jobs", params={"limit": 100}, caller_headers=caller_headers)
    info_task = client.request("GET", "/api/infographics/runs", caller_headers=caller_headers)
    scenario_task = client.probe_optional("/api/scenario/health", caller_headers=caller_headers)
    prompts_task = client.probe_optional("/api/prompts/health", caller_headers=caller_headers)
    voice_task = client.probe_optional("/api/voice/queue", caller_headers=caller_headers)
    footage_task = client.probe_optional("/api/footage/health", caller_headers=caller_headers)

    health, jobs_res, info_res, scenario_h, prompts_h, voice_q, footage_h = await asyncio.gather(
        health_task,
        jobs_task,
        info_task,
        scenario_task,
        prompts_task,
        voice_task,
        footage_task,
        return_exceptions=True,
    )

    jobs: list[dict[str, Any]] = jobs_res if isinstance(jobs_res, list) else []
    by_state: dict[str, int] = {}
    active_jobs: list[dict[str, Any]] = []
    for job in jobs:
        st = str(job.get("state", "unknown"))
        by_state[st] = by_state.get(st, 0) + 1
        if st in ("running", "waiting", "queued"):
            active_jobs.append({
                "id": job["id"],
                "title": job.get("title"),
                "mode": job.get("mode"),
                "state": st,
                "progress": job.get("progress"),
                "message": job.get("message"),
            })

    infographic_runs: list[dict[str, Any]] = info_res if isinstance(info_res, list) else []
    active_infographics = [
        {
            "id": r["id"],
            "scenario_name": r.get("scenario_name"),
            "state": r.get("state"),
            "done": r.get("done"),
            "total": r.get("total"),
            "found": r.get("found"),
        }
        for r in infographic_runs
        if r.get("state") not in ("done", "failed")
    ]

    accounts_summary: dict[str, Any]
    try:
        accs: list[dict[str, Any]] = await client.request("GET", "/api/accounts", caller_headers=caller_headers)
        enabled = [a for a in accs if a.get("enabled", True)]
        ready_all = [
            a
            for a in enabled
            if a.get("cooldown", 0) <= 0 and a.get("image_pause", 0) <= 0 and a.get("video_pause", 0) <= 0
        ]
        accounts_summary = {
            "provider": accs[0].get("provider", client.settings.browser_provider) if accs else client.settings.browser_provider,
            "total": len(accs),
            "enabled": len(enabled),
            "fully_ready": len(ready_all),
            "on_account_cooldown": sum(1 for a in enabled if a.get("cooldown", 0) > 0),
            "image_paused": sum(1 for a in enabled if a.get("image_pause", 0) > 0),
            "video_paused": sum(1 for a in enabled if a.get("video_pause", 0) > 0),
            "dead_proxies_total": sum(int(a.get("dead_proxies", 0)) for a in accs),
        }
    except PlatformAPIError as exc:
        accounts_summary = {"visible": False, "reason": exc.detail}

    local_prompt_runs_active = 0
    if isinstance(prompts_h, dict) and prompts_h.get("state") in ("not_configured", "not_mounted"):
        try:
            local_runs = await client.request("GET", "/api/prompts/runs", caller_headers=caller_headers)
            if isinstance(local_runs, list):
                local_prompt_runs_active = sum(1 for r in local_runs if r.get("state") in ("queued", "running"))
                prompts_h = {
                    "service": "fl-api-prompts",
                    "state": "online_builtin",
                    "active_runs": local_prompt_runs_active,
                    "recent_runs": len(local_runs),
                }
        except PlatformAPIError:
            pass

    return {
        "core_health": health if isinstance(health, dict) else {"state": "error", "detail": str(health)},
        "job_queue": {
            "recent_counts_by_state": by_state,
            "active_and_queued": active_jobs[:15],
        },
        "accounts": accounts_summary,
        "infographics": {
            "recent_total": len(infographic_runs),
            "active": active_infographics,
        },
        "upstream_services": {
            "scenario": scenario_h if isinstance(scenario_h, dict) else {"state": "error"},
            "prompts": prompts_h if isinstance(prompts_h, dict) else {"state": "error"},
            "voice": voice_q if isinstance(voice_q, dict) else {"state": "error"},
            "footage": footage_h if isinstance(footage_h, dict) else {"state": "error"},
        },
    }


def register_monitor_tools(mcp: MCPServer, client: FlPlatformClient) -> None:
    """Register monitoring, account pool, and diagnostic tools on `mcp`."""

    @mcp.tool(
        title="System Overview",
        annotations=READ_ONLY,
    )
    async def get_system_overview(ctx: Context | None = None) -> dict[str, Any]:
        """Get a complete live dashboard of queues, active Veo/image jobs, accounts, infographics, and upstream services."""
        return await build_system_overview(client, _ctx_headers(ctx))

    @mcp.tool(
        title="Accounts & Proxies Health",
        annotations=READ_ONLY,
    )
    async def get_accounts_status(ctx: Context | None = None) -> dict[str, Any]:
        """List all Dolphin Anty / Octo Browser accounts with cooldowns, image/video quotas, active image models, and dead proxies (admin only)."""
        accounts: list[dict[str, Any]] = await client.request("GET", "/api/accounts", caller_headers=_ctx_headers(ctx))
        return {
            "count": len(accounts),
            "image_model_pool": client.settings.image_models,
            "max_active_browsers": client.settings.max_active_browsers,
            "accounts": accounts,
        }

    @mcp.tool(
        title="Reset Account Cooldown",
        annotations=SAFE_MUTATE,
    )
    async def reset_account_cooldown(
        account_name: str,
        kind: Literal["all", "account", "image", "video"] = "all",
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Clear cooldown timers ('all', 'account', 'image', or 'video') on a specific account such as 'account_1' (admin only)."""
        return await client.request(
            "POST",
            f"/api/accounts/{account_name}/reset-cooldown",
            json_body={"kind": kind},
            caller_headers=_ctx_headers(ctx),
        )

    @mcp.tool(
        title="Revive Dead Proxies",
        annotations=SAFE_MUTATE,
    )
    async def revive_account_proxies(
        account_name: str,
        proxy: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Remove a `host:port` proxy (or all dead proxies if `proxy` is omitted) from dead_proxies.txt on an account (admin only)."""
        return await client.request(
            "POST",
            f"/api/accounts/{account_name}/revive-proxies",
            json_body={"proxy": proxy},
            caller_headers=_ctx_headers(ctx),
        )

    @mcp.tool(
        title="Tail Server Logs",
        annotations=READ_ONLY,
    )
    async def tail_server_logs(
        lines: int = 80,
        level: Literal["DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"] | None = None,
        search: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Fetch sanitized recent log entries from the latest server log file, optionally filtered by level or substring (admin only)."""
        return await client.request(
            "GET",
            "/api/admin/logs",
            params={"lines": min(max(lines, 1), 300), "level": level, "search": search},
            caller_headers=_ctx_headers(ctx),
        )

    @mcp.tool(
        title="Diagnose Job",
        annotations=READ_ONLY,
    )
    async def diagnose_job(
        job_id: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Analyze why a job is waiting, stopped, or failed: combines job status, skipped row reasons, account pauses, and matching logs."""
        headers = _ctx_headers(ctx)
        detail = await client.request("GET", f"/api/jobs/{job_id}", caller_headers=headers)
        files: list[dict[str, Any]] = await client.request("GET", f"/api/jobs/{job_id}/files", caller_headers=headers)

        skips = [
            {"row": f["row"], "reason": f.get("reason") or "skipped"}
            for f in files
            if f.get("kind") == "skip"
        ]
        done_rows = {f["row"] for f in files if f.get("kind") in ("video", "image", "skip")}
        missing_rows = [r["name"] for r in detail.get("rows", []) if r["name"] not in done_rows]

        account_blockers: list[str] = []
        try:
            accs: list[dict[str, Any]] = await client.request("GET", "/api/accounts", caller_headers=headers)
            enabled = [a for a in accs if a.get("enabled", True)]
            if not enabled:
                account_blockers.append("No enabled browser accounts configured.")
            else:
                if all(a.get("cooldown", 0) > 0 for a in enabled):
                    min_cd = min(a.get("cooldown", 0) for a in enabled)
                    account_blockers.append(f"All {len(enabled)} enabled accounts are on full cooldown (earliest in {min_cd:.0f}s).")
                if detail.get("mode") in ("img", "i2v", "flash_i2v", "i2i2v") and all(
                    a.get("image_pause", 0) > 0 or a.get("image_quota_limited") for a in enabled
                ):
                    account_blockers.append("All enabled accounts have hit rolling image generation quotas.")
                if detail.get("mode") != "img" and all(a.get("video_pause", 0) > 0 for a in enabled):
                    account_blockers.append("All enabled accounts have video model access paused (PUBLIC_ERROR_MODEL_ACCESS_DENIED).")
                exhausted_proxies = [
                    a["name"] for a in enabled if a.get("proxies", 0) > 0 and a.get("dead_proxies", 0) >= a.get("proxies", 0)
                ]
                if exhausted_proxies:
                    account_blockers.append(f"All proxies marked dead on: {', '.join(exhausted_proxies)}.")
        except PlatformAPIError:
            pass

        recent_logs: list[str] = []
        try:
            log_res = await client.request(
                "GET",
                "/api/admin/logs",
                params={"lines": 40, "search": job_id},
                caller_headers=headers,
            )
            recent_logs = log_res.get("lines", [])
            if not recent_logs:
                warn_res = await client.request(
                    "GET",
                    "/api/admin/logs",
                    params={"lines": 25, "level": "WARNING"},
                    caller_headers=headers,
                )
                recent_logs = warn_res.get("lines", [])
        except PlatformAPIError:
            pass

        state = detail.get("state")
        can_retry = state in ("stopped", "failed", "cancelled")
        return {
            "job": {
                "id": detail["id"],
                "title": detail.get("title"),
                "mode": detail.get("mode"),
                "strategy": detail.get("strategy"),
                "state": state,
                "position": detail.get("position"),
                "progress": detail.get("progress"),
                "message": detail.get("message"),
                "created_at": detail.get("created_at"),
                "started_at": detail.get("started_at"),
                "finished_at": detail.get("finished_at"),
            },
            "can_retry": can_retry,
            "skipped_count": len(skips),
            "skipped_rows": skips[:50],
            "unfinished_row_names_sample": missing_rows[:50],
            "unfinished_count": len(missing_rows),
            "account_blockers": account_blockers,
            "recent_logs": recent_logs,
        }
