"""MCP tools for Veo video & Nano Banana image generation jobs (JobQueue)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import ToolAnnotations

from src.jobs.modes import MODES, Mode, Strategy
from src.mcp.client import FlPlatformClient, extract_caller_headers, with_style

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True)
CREATE_ACTION = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False)
SAFE_MUTATE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True)


def _ctx_headers(ctx: Context | None):
    return extract_caller_headers(ctx)


async def resolve_project_style(client: FlPlatformClient, project_id: str | None, caller_headers=None) -> str:
    """Look up a project's style string so MCP jobs match frontend prompt enrichment."""
    if not project_id:
        return ""
    projects: list[dict[str, Any]] = await client.request("GET", "/api/projects", caller_headers=caller_headers)
    for proj in projects:
        if proj.get("id") == project_id:
            return str(proj.get("style") or "").strip()
    return ""


async def resolve_avatar_refs(
    client: FlPlatformClient,
    avatar_id: str | None,
    avatar_key: str | None,
    avatar_keys: list[str] | None = None,
    caller_headers=None,
) -> tuple[str | None, list[str] | None, str]:
    """Resolve `(primary_key, all_keys, appearance)` from an `avatar_id` or raw `avatar_key` / `avatar_keys`."""
    explicit = [str(k) for k in (avatar_keys or []) if k]
    if avatar_key and avatar_key not in explicit:
        explicit.insert(0, avatar_key)
    if explicit:
        return explicit[0], explicit, ""
    if not avatar_id:
        return None, None, ""
    avatars: list[dict[str, Any]] = await client.request("GET", "/api/avatars", caller_headers=caller_headers)
    for av in avatars:
        if av.get("id") == avatar_id:
            keys = [str(k) for k in (av.get("image_keys") or []) if k]
            if not keys:
                raise ValueError(f"Avatar {avatar_id} has no reference images yet")
            return keys[0], keys, str(av.get("appearance") or "").strip()
    raise ValueError(f"Avatar {avatar_id} not found")


async def resolve_avatar(
    client: FlPlatformClient, avatar_id: str | None, avatar_key: str | None, caller_headers=None
) -> tuple[str | None, str]:
    """Resolve `(image_key, appearance)` from an `avatar_id` or raw `avatar_key`."""
    primary, _, appearance = await resolve_avatar_refs(client, avatar_id, avatar_key, caller_headers=caller_headers)
    return primary, appearance


def enrich_job_rows(
    raw_rows: list[dict[str, Any]],
    *,
    mode: Mode,
    style: str = "",
    avatar_appearance: str = "",
    use_static_image: bool = False,
) -> list[dict[str, str | None]]:
    """Apply mode rules, avatar appearance, and project style suffix to job rows."""
    spec = MODES[mode]
    out: list[dict[str, str | None]] = []
    for idx, row in enumerate(raw_rows, start=1):
        name = str(row.get("name") or idx).strip()
        vid = (row.get("prompt") or "").strip() or None if spec.needs_video_prompt else None
        img = (row.get("image_prompt") or "").strip() or None if (spec.needs_image_prompt and not use_static_image) else None
        if spec.avatar and avatar_appearance and img:
            img = f"{img}. {avatar_appearance}"
        if style:
            vid = with_style(vid, style)
            img = with_style(img, style)
        out.append({"name": name, "prompt": vid, "image_prompt": img})
    return out


def build_modes_catalog() -> list[dict[str, Any]]:
    """Build complete metadata for all Google Flow generation modes, including durations and aspect ratios."""
    out: list[dict[str, Any]] = []
    for mode, spec in MODES.items():
        entry: dict[str, Any] = {
            "id": mode.value,
            "title": spec.title,
            "makes_images": spec.makes_images,
            "makes_videos": spec.makes_videos,
            "needs_video_prompt": spec.needs_video_prompt,
            "needs_image_prompt": spec.needs_image_prompt,
            "avatar": spec.avatar,
            "optional_avatar": spec.optional_avatar,
            "static_image": spec.static_image,
            "end_image": spec.end_image,
            "requires_frames": spec.requires_frames,
            "chain": spec.chain,
            "durations": list(spec.durations),
            "default_duration": spec.default_duration,
            "aspect_ratios": list(spec.aspect_ratios),
            "default_aspect_ratio": spec.default_aspect_ratio,
        }
        if spec.duration_models:
            entry["video_models_by_duration"] = {str(k): v for k, v in spec.duration_models.items()}
        if spec.frame_duration_models:
            entry["frame_models_by_duration"] = {str(k): v for k, v in spec.frame_duration_models.items()}
        if spec.interpolation_duration_models:
            entry["interpolation_models_by_duration"] = {
                str(k): v for k, v in spec.interpolation_duration_models.items()
            }
        out.append(entry)
    return out


def register_job_tools(mcp: MCPServer, client: FlPlatformClient) -> None:
    """Register Veo / image batch queue tools on `mcp`."""

    @mcp.tool(
        title="List Generation Modes",
        annotations=READ_ONLY,
    )
    async def list_generation_modes(ctx: Context | None = None) -> list[dict[str, Any]]:
        """List all Google Flow generation modes (t2v, flash_t2v, img, i2v, fl2v, flash_i2v, i2i2v, f2v), supported video durations (4/6/8/10s), aspect ratios (16:9, 9:16, 1:1), and required inputs."""
        api_modes: list[dict[str, Any]] = await client.request("GET", "/api/modes", caller_headers=_ctx_headers(ctx))
        by_id = {m["id"]: m for m in build_modes_catalog()}
        return [{**by_id.get(m["id"], {}), **m} for m in api_modes]

    @mcp.tool(
        title="List Generation Jobs",
        annotations=READ_ONLY,
    )
    async def list_jobs(
        project_id: str | None = None,
        state: Literal["queued", "running", "waiting", "done", "stopped", "failed", "cancelled"] | None = None,
        limit: int = 30,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """List queued, active, or historical generation jobs, optionally filtered by `project_id` and `state`."""
        fetch_limit = min(max(limit * 3 if state else limit, 1), 500)
        jobs: list[dict[str, Any]] = await client.request(
            "GET",
            "/api/jobs",
            params={"limit": fetch_limit, "project_id": project_id},
            caller_headers=_ctx_headers(ctx),
        )
        if state:
            jobs = [j for j in jobs if j.get("state") == state]
        capped = jobs[: min(max(limit, 1), 200)]
        return {"count": len(capped), "jobs": capped}

    @mcp.tool(
        title="Get Job Details & Files",
        annotations=READ_ONLY,
    )
    async def get_job_details(
        job_id: str,
        row_filter: Literal["summary", "all", "done", "pending", "skipped"] = "summary",
        offset: int = 0,
        limit: int = 40,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Get full job progress, queue position, skipped row reasons, and paginated rows/result links without overflowing context."""
        headers = _ctx_headers(ctx)
        detail, files = await asyncio.gather(
            client.request("GET", f"/api/jobs/{job_id}", caller_headers=headers),
            client.request("GET", f"/api/jobs/{job_id}/files", caller_headers=headers),
        )
        files_by_row: dict[str, list[dict[str, Any]]] = {}
        for f in files:
            files_by_row.setdefault(f["row"], []).append(f)

        all_rows: list[dict[str, Any]] = detail.get("rows", [])
        merged: list[dict[str, Any]] = []
        skipped_items: list[dict[str, Any]] = []

        for r in all_rows:
            rname = r["name"]
            rfiles = files_by_row.get(rname, [])
            kinds = {f["kind"] for f in rfiles}
            if "skip" in kinds:
                row_state = "skipped"
                reason = next((f.get("reason") for f in rfiles if f["kind"] == "skip"), None)
                skipped_items.append({"name": rname, "reason": reason})
            elif "video" in kinds or (detail.get("mode") == "img" and "image" in kinds):
                row_state = "done"
            else:
                row_state = "pending"
            merged.append({
                "name": rname,
                "state": row_state,
                "prompt": r.get("prompt"),
                "image_prompt": r.get("image_prompt"),
                "files": [
                    {"name": f["name"], "kind": f["kind"], "url": f.get("url"), "reason": f.get("reason")}
                    for f in rfiles
                ],
            })

        if row_filter == "done":
            filtered = [m for m in merged if m["state"] == "done"]
        elif row_filter == "pending":
            filtered = [m for m in merged if m["state"] == "pending"]
        elif row_filter == "skipped":
            filtered = [m for m in merged if m["state"] == "skipped"]
        elif row_filter == "all":
            filtered = merged
        else:
            # summary: return up to `limit` items prioritizing skipped + done sample
            filtered = merged

        page_size = min(max(limit, 1), 100)
        start = max(offset, 0)
        page = filtered[start : start + page_size]

        status_copy = {k: v for k, v in detail.items() if k != "rows"}
        return {
            "job": status_copy,
            "total_rows": len(all_rows),
            "skipped_rows": skipped_items[:50],
            "pagination": {
                "filter": row_filter,
                "offset": start,
                "limit": page_size,
                "returned": len(page),
                "total_matching": len(filtered),
            },
            "rows": page,
        }

    @mcp.tool(
        title="Upload Input Image",
        annotations=CREATE_ACTION,
    )
    async def upload_input_image(
        file_path: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Upload a local picture (.jpg, .jpeg, .png, .webp) to the object store for use as a static_image, end_image, or avatar reference."""
        path = Path(file_path).expanduser().resolve()
        if not path.is_file():
            raise ValueError(f"Local file not found: {path}")
        content = await asyncio.to_thread(path.read_bytes)
        return await client.request(
            "POST",
            "/api/uploads",
            files={"file": (path.name, content)},
            caller_headers=_ctx_headers(ctx),
        )

    @mcp.tool(
        title="Submit Generation Job",
        annotations=CREATE_ACTION,
    )
    async def submit_generation_job(
        mode: Mode,
        rows: list[dict[str, str | None]] | None = None,
        prompts: list[str] | None = None,
        xlsx_path: str | None = None,
        title: str | None = None,
        project_id: str | None = None,
        strategy: Strategy | None = None,
        duration: Literal[4, 6, 8, 10] | None = None,
        aspect_ratio: Literal["16:9", "9:16", "1:1"] | None = None,
        avatar_id: str | None = None,
        avatar_key: str | None = None,
        avatar_keys: list[str] | None = None,
        static_image_key: str | None = None,
        end_image_key: str | None = None,
        video_model_key: str | None = None,
        apply_project_style: bool = True,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """
        Submit a Veo video or image generation job to JobQueue.

        Provide input rows in ONE of three ways:
        - `rows`: explicit `[{"name": "1", "prompt": "video prompt", "image_prompt": "image prompt"}]`
        - `prompts`: simple list of prompt strings `["shot 1...", "shot 2..."]` (auto-numbered 1..N)
        - `xlsx_path`: path to a local `.xlsx` file with `number`, `prompt` / `img_prompt` + `vid_prompt` columns.

        Keyframe interpolation (Start + End Frame):
        - Use `mode="fl2v"` (or `mode="i2v"`) with both `static_image_key` (start frame) and `end_image_key` (end frame)
          for 3D camera orbits and smooth transitions between two keyframes (`duration`: `4`, `6`, or `8` sec).

        Video duration (`duration`): `4`, `6`, or `8` sec for Veo 3.1 (`t2v`, `i2v`, `fl2v`, `i2i2v`, default `8`);
        `4`, `6`, `8`, or `10` sec for Omni 1.1 Flash (`flash_t2v`, `flash_i2v`, default `10`) and chained `f2v` (default `8`).
        Aspect ratio (`aspect_ratio`): `"16:9"` or `"9:16"` for video modes, plus `"1:1"` for `img`.
        """
        headers = _ctx_headers(ctx)
        spec = MODES[mode]
        sources_given = sum(1 for x in (rows, prompts, xlsx_path) if x)
        if sources_given != 1:
            raise ValueError("Provide exactly one of `rows`, `prompts`, or `xlsx_path`.")

        raw_rows: list[dict[str, Any]]
        if xlsx_path:
            path = Path(xlsx_path).expanduser().resolve()
            if not path.is_file():
                raise ValueError(f"Excel file not found: {path}")
            parsed = await client.request(
                "POST",
                "/api/rows/xlsx",
                data={"mode": mode.value},
                files={"file": (path.name, await asyncio.to_thread(path.read_bytes))},
                caller_headers=headers,
            )
            raw_rows = parsed["rows"]
            if not title:
                title = path.stem
        elif prompts:
            clean = [p.strip() for p in prompts if p and p.strip()]
            if not clean:
                raise ValueError("`prompts` contains no non-empty strings.")
            raw_rows = [
                {
                    "name": str(i),
                    "prompt": p if spec.needs_video_prompt else None,
                    "image_prompt": p if spec.needs_image_prompt else None,
                }
                for i, p in enumerate(clean, start=1)
            ]
        else:
            assert rows is not None
            raw_rows = rows

        resolved_avatar_key, resolved_avatar_keys, avatar_appearance = await resolve_avatar_refs(
            client, avatar_id, avatar_key, avatar_keys=avatar_keys, caller_headers=headers
        )
        style = await resolve_project_style(client, project_id, headers) if apply_project_style else ""
        enriched_rows = enrich_job_rows(
            raw_rows,
            mode=mode,
            style=style,
            avatar_appearance=avatar_appearance,
            use_static_image=bool(static_image_key),
        )
        chosen_strategy = strategy or (Strategy.SEQUENTIAL if spec.chain else Strategy.PARALLEL)

        body: dict[str, Any] = {
            "mode": mode.value,
            "rows": enriched_rows,
            "title": title,
            "project_id": project_id,
            "strategy": chosen_strategy.value,
            "duration": duration,
            "aspect_ratio": aspect_ratio,
            "avatar": resolved_avatar_key,
            "avatar_keys": resolved_avatar_keys,
            "static_image": static_image_key,
            "end_image": end_image_key,
            "video_model_key": video_model_key,
        }
        return await client.request("POST", "/api/jobs", json_body=body, caller_headers=headers)

    @mcp.tool(
        title="Cancel or Retry Job",
        annotations=SAFE_MUTATE,
    )
    async def control_job(
        job_id: str,
        action: Literal["cancel", "retry"],
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Cancel a queued/running job, or retry a stopped/failed/cancelled job (only unfinished rows run)."""
        return await client.request("POST", f"/api/jobs/{job_id}/{action}", caller_headers=_ctx_headers(ctx))

    @mcp.tool(
        title="Delete Job",
        annotations=DESTRUCTIVE,
    )
    async def delete_job(
        job_id: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Permanently delete a job and all its generated images/videos from storage."""
        return await client.request("DELETE", f"/api/jobs/{job_id}", caller_headers=_ctx_headers(ctx))

    @mcp.tool(
        title="Sync Jobs From Object Store",
        annotations=SAFE_MUTATE,
    )
    async def sync_jobs_from_storage(ctx: Context | None = None) -> dict[str, Any]:
        """Restore missing job records in the database from `jobs/*/meta.json` in the object store (admin only)."""
        return await client.request("POST", "/api/jobs/sync", caller_headers=_ctx_headers(ctx))
