"""MCP tools for Projects, Avatars, Scenario, Prompts, Infographics, Voice, and Footage."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import ToolAnnotations

from src.jobs.modes import MODES, Mode, Strategy
from src.mcp.client import FlPlatformClient, PlatformAPIError, docx_bytes_from_text, extract_caller_headers
from src.mcp.tools_jobs import enrich_job_rows, resolve_avatar_refs, resolve_project_style
from src.prompts.scenario import parse_scenario

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True)
CREATE_ACTION = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False)
SAFE_MUTATE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True)


def _ctx_headers(ctx: Context | None):
    return extract_caller_headers(ctx)


async def _scenario_docx_bytes(
    client: FlPlatformClient,
    *,
    docx_path: str | None = None,
    scenario_text: str | None = None,
    scenario_project_id: str | None = None,
    default_title: str = "scenario",
    caller_headers=None,
) -> tuple[str, bytes]:
    """Obtain `(filename, docx_bytes)` from a local `.docx`, raw text, or a finished `fl-scenario` project."""
    given = sum(1 for x in (docx_path, scenario_text, scenario_project_id) if x)
    if given != 1:
        raise ValueError("Provide exactly one of `docx_path`, `scenario_text`, or `scenario_project_id`.")

    if docx_path:
        path = Path(docx_path).expanduser().resolve()
        if not path.is_file():
            raise ValueError(f"File not found: {path}")
        raw = await asyncio.to_thread(path.read_bytes)
        if path.suffix.lower() == ".docx":
            return path.name, raw
        text = raw.decode("utf-8", errors="replace")
        return f"{path.stem}.docx", await asyncio.to_thread(docx_bytes_from_text, text)

    if scenario_text:
        stem = (default_title or "scenario").strip() or "scenario"
        return f"{stem}.docx", await asyncio.to_thread(docx_bytes_from_text, scenario_text)

    assert scenario_project_id is not None
    proj = await client.request("GET", f"/api/scenario/projects/{scenario_project_id}", caller_headers=caller_headers)
    title = str(proj.get("title") or default_title or scenario_project_id).strip()
    files = (proj.get("job") or {}).get("files") or []
    docx_file = next((f["name"] for f in files if str(f.get("name", "")).lower().endswith(".docx")), None)
    if docx_file:
        data, _ = await client.request_bytes(
            "GET",
            f"/api/scenario/projects/{scenario_project_id}/files/{docx_file}",
            caller_headers=caller_headers,
        )
        return docx_file, data

    script = await client.request("GET", f"/api/scenario/projects/{scenario_project_id}/script", caller_headers=caller_headers)
    paragraphs: list[str] = []
    for ch in script.get("chapters", []):
        for p in ch.get("paragraphs", []):
            txt = str(p.get("text") or "").strip()
            if txt:
                paragraphs.append(txt)
    if not paragraphs:
        raise ValueError(f"Scenario project {scenario_project_id} has no finished script paragraphs yet.")
    return f"{title}.docx", await asyncio.to_thread(docx_bytes_from_text, paragraphs)


def register_pipeline_tools(mcp: MCPServer, client: FlPlatformClient) -> None:
    """Register Projects, Avatars, and multi-stage video pipeline tools on `mcp`."""

    # ------------------------------------------------------------------ Projects & Avatars

    @mcp.tool(title="List Projects", annotations=READ_ONLY)
    async def list_projects(ctx: Context | None = None) -> list[dict[str, Any]]:
        """List all video projects («Ролики») and their prompt style suffixes."""
        return await client.request("GET", "/api/projects", caller_headers=_ctx_headers(ctx))

    @mcp.tool(title="Create or Update Project", annotations=SAFE_MUTATE)
    async def save_project(
        title: str | None = None,
        style: str | None = None,
        project_id: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Create a new video project (when `project_id` is omitted) or update an existing project's `title` and `style`."""
        headers = _ctx_headers(ctx)
        if project_id:
            patch = {k: v for k, v in (("title", title), ("style", style)) if v is not None}
            return await client.request("PATCH", f"/api/projects/{project_id}", json_body=patch, caller_headers=headers)
        if not title or not title.strip():
            raise ValueError("`title` is required when creating a new project.")
        return await client.request(
            "POST",
            "/api/projects",
            json_body={"title": title.strip(), "style": style or ""},
            caller_headers=headers,
        )

    @mcp.tool(title="Delete Project", annotations=DESTRUCTIVE)
    async def delete_project(project_id: str, ctx: Context | None = None) -> dict[str, Any]:
        """Delete a project record by ID."""
        return await client.request("DELETE", f"/api/projects/{project_id}", caller_headers=_ctx_headers(ctx))

    @mcp.tool(title="List Avatars", annotations=READ_ONLY)
    async def list_avatars(ctx: Context | None = None) -> list[dict[str, Any]]:
        """List all character avatars with their appearance descriptions and reference image keys."""
        return await client.request("GET", "/api/avatars", caller_headers=_ctx_headers(ctx))

    @mcp.tool(title="Create or Update Avatar", annotations=SAFE_MUTATE)
    async def save_avatar(
        name: str | None = None,
        appearance: str | None = None,
        image_keys: list[str] | None = None,
        local_image_paths: list[str] | None = None,
        avatar_id: str | None = None,
        generate_360: bool = False,
        project_id: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Create or update an avatar for `i2i2v` mode; local pictures in `local_image_paths` are uploaded automatically. Set `generate_360=True` to build 360° turnaround angles (or generate from text if no picture is given)."""
        headers = _ctx_headers(ctx)
        keys = list(image_keys or [])
        for raw_path in local_image_paths or []:
            path = Path(raw_path).expanduser().resolve()
            if not path.is_file():
                raise ValueError(f"Avatar image file not found: {path}")
            uploaded = await client.request(
                "POST",
                "/api/uploads",
                files={"file": (path.name, await asyncio.to_thread(path.read_bytes))},
                caller_headers=headers,
            )
            keys.append(uploaded["key"])

        if avatar_id:
            patch: dict[str, Any] = {}
            if name is not None:
                patch["name"] = name
            if appearance is not None:
                patch["appearance"] = appearance
            if keys:
                patch["image_keys"] = keys
            saved = await client.request("PATCH", f"/api/avatars/{avatar_id}", json_body=patch, caller_headers=headers) if patch else await client.request("GET", "/api/avatars", caller_headers=headers)
            if generate_360:
                return await client.request(
                    "POST",
                    f"/api/avatars/{avatar_id}/angles",
                    json_body={"project_id": project_id},
                    caller_headers=headers,
                )
            return saved if isinstance(saved, dict) else next(a for a in saved if a["id"] == avatar_id)

        if not keys and generate_360:
            if not name or not (appearance and appearance.strip()):
                raise ValueError("Generating an avatar from scratch (`generate_360=True` without images) requires `name` and `appearance`.")
            return await client.request(
                "POST",
                "/api/avatars/generate",
                json_body={"name": name, "appearance": appearance.strip(), "project_id": project_id},
                caller_headers=headers,
            )

        if not name or not keys:
            raise ValueError("Creating an avatar requires `name` and at least one image in `image_keys` or `local_image_paths` (or `generate_360=True` with `appearance`).")
        saved = await client.request(
            "POST",
            "/api/avatars",
            json_body={"name": name, "appearance": appearance or "", "image_keys": keys},
            caller_headers=headers,
        )
        if generate_360:
            return await client.request(
                "POST",
                f"/api/avatars/{saved['id']}/angles",
                json_body={"project_id": project_id},
                caller_headers=headers,
            )
        return saved

    @mcp.tool(title="Delete Avatar", annotations=DESTRUCTIVE)
    async def delete_avatar(avatar_id: str, ctx: Context | None = None) -> dict[str, Any]:
        """Delete an avatar by ID."""
        return await client.request("DELETE", f"/api/avatars/{avatar_id}", caller_headers=_ctx_headers(ctx))

    @mcp.tool(title="Analyze Avatar Reference Photo", annotations=CREATE_ACTION)
    async def analyze_avatar_reference(
        image_key: str | None = None,
        file_path: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Classify framing (`close_up`, `medium`, `full_body`, `turnaround_sheet`) and camera angle (`front`, `three_quarter`, `profile`, `back`) of an uploaded `image_key` or local `file_path`, returning non-duplicating `recommended_angles` for 360° generation."""
        if bool(image_key) == bool(file_path):
            raise ValueError("Provide exactly one of `image_key` or `file_path`.")
        headers = _ctx_headers(ctx)
        resolved_key = image_key
        if file_path:
            path = Path(file_path).expanduser().resolve()
            if not path.is_file():
                raise ValueError(f"Reference image file not found: {path}")
            uploaded = await client.request(
                "POST",
                "/api/uploads",
                files={"file": (path.name, await asyncio.to_thread(path.read_bytes))},
                caller_headers=headers,
            )
            resolved_key = uploaded["key"]
        return await client.request(
            "POST",
            "/api/avatars/analyze-reference",
            json_body={"key": resolved_key},
            caller_headers=headers,
        )

    @mcp.tool(title="Order Avatar References Canonically", annotations=SAFE_MUTATE)
    async def order_avatar_references(
        image_keys: list[str],
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Automatically sort uploaded reference `image_keys` into canonical Google Flow & Veo slot priority (`0° front -> 45° three_quarter -> full_body -> 90° profile -> 180° back`)."""
        if not image_keys:
            raise ValueError("`image_keys` must contain at least one uploaded key.")
        return await client.request(
            "POST",
            "/api/avatars/order-references",
            json_body={"keys": image_keys},
            caller_headers=_ctx_headers(ctx),
        )

    @mcp.tool(title="Generate 360° Avatar Turnaround Angles", annotations=CREATE_ACTION)
    async def generate_avatar_angles(
        avatar_id: str | None = None,
        name: str | None = None,
        appearance: str | None = None,
        angles: list[Literal["front", "three_quarter", "profile", "back", "full_body"]] | None = None,
        style: str | None = None,
        project_id: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Generate studio 360° turnaround angles (`three_quarter`, `profile`, `back`, `full_body`) for an existing `avatar_id` locked to its primary photo, or create a brand-new avatar from text (`name` + `appearance`) when `avatar_id` is omitted."""
        headers = _ctx_headers(ctx)
        if avatar_id:
            body: dict[str, Any] = {}
            if angles is not None:
                body["angles"] = angles
            if style is not None:
                body["style"] = style
            if project_id is not None:
                body["project_id"] = project_id
            return await client.request(
                "POST",
                f"/api/avatars/{avatar_id}/angles",
                json_body=body,
                caller_headers=headers,
            )
        if not name or not (appearance and appearance.strip()):
            raise ValueError("Provide `avatar_id` for an existing avatar, or both `name` and `appearance` to generate from scratch.")
        gen_body: dict[str, Any] = {"name": name.strip(), "appearance": appearance.strip()}
        if angles is not None:
            gen_body["angles"] = angles
        if style is not None:
            gen_body["style"] = style
        if project_id is not None:
            gen_body["project_id"] = project_id
        return await client.request(
            "POST",
            "/api/avatars/generate",
            json_body=gen_body,
            caller_headers=headers,
        )

    @mcp.tool(title="Cancel 360° Avatar Generation", annotations=SAFE_MUTATE)
    async def cancel_avatar_angles(
        avatar_id: str,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Stop an active 360° turnaround generation job for `avatar_id` while keeping any angles already imported."""
        return await client.request(
            "POST",
            f"/api/avatars/{avatar_id}/angles/cancel",
            json_body={},
            caller_headers=_ctx_headers(ctx),
        )

    # ------------------------------------------------------------------ Stage 1: Scenario & Analytics (fl-scenario)

    @mcp.tool(title="List Scenario Niches & Options", annotations=READ_ONLY)
    async def get_scenario_options(ctx: Context | None = None) -> dict[str, Any]:
        """Fetch available niches, models, languages, and tones from `fl-scenario`."""
        return await client.request("GET", "/api/scenario/options", caller_headers=_ctx_headers(ctx))

    @mcp.tool(title="List Scenario Analytics", annotations=READ_ONLY)
    async def list_scenario_analytics(
        niche: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """List running and completed YouTube topic analytics runs in `fl-scenario`."""
        return await client.request(
            "GET",
            "/api/scenario/analytics",
            params={"niche": niche},
            caller_headers=_ctx_headers(ctx),
        )

    @mcp.tool(title="Start Scenario Analytics", annotations=CREATE_ACTION)
    async def start_scenario_analytics(
        niche: str | None = None,
        urls: list[str] | None = None,
        notes: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Launch a topic/competitor analytics task in `fl-scenario`."""
        body = {k: v for k, v in (("niche", niche), ("urls", urls), ("notes", notes)) if v is not None}
        return await client.request("POST", "/api/scenario/analytics", json_body=body, caller_headers=_ctx_headers(ctx))

    @mcp.tool(title="List Scenario Projects", annotations=READ_ONLY)
    async def list_scenario_projects(ctx: Context | None = None) -> dict[str, Any]:
        """List scriptwriting projects in `fl-scenario` with their stage progress (brief -> plan -> chapters -> done)."""
        projects: list[dict[str, Any]] = await client.request(
            "GET", "/api/scenario/projects", caller_headers=_ctx_headers(ctx)
        )
        compact = [
            {
                "id": p.get("id"),
                "title": p.get("title"),
                "niche": p.get("niche"),
                "status": p.get("status"),
                "model": p.get("model"),
                "stages": p.get("stages"),
                "job_progress": (p.get("job") or {}).get("progress"),
                "updated": p.get("updated"),
            }
            for p in projects
        ]
        return {"count": len(compact), "projects": compact}

    @mcp.tool(title="Get Scenario Project Details", annotations=READ_ONLY)
    async def get_scenario_project(
        scenario_project_id: str,
        include_script: bool = False,
        include_logs: bool = False,
        max_paragraphs: int = 40,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Inspect a `fl-scenario` project's brief, stages, job progress, and optionally paginated script paragraphs or logs."""
        headers = _ctx_headers(ctx)
        proj = await client.request("GET", f"/api/scenario/projects/{scenario_project_id}", caller_headers=headers)
        out: dict[str, Any] = {"project": proj}
        if include_script and proj.get("status") == "done":
            script = await client.request(
                "GET", f"/api/scenario/projects/{scenario_project_id}/script", caller_headers=headers
            )
            flat: list[dict[str, Any]] = []
            for ch in script.get("chapters", []):
                for p in ch.get("paragraphs", []):
                    flat.append({"chapter": ch.get("number"), "chapter_title": ch.get("title"), "n": p.get("n"), "text": p.get("text")})
            out["script_meta"] = {
                "title": script.get("title"),
                "language": script.get("language"),
                "logline": script.get("logline"),
                "total_paragraphs": len(flat),
            }
            out["paragraphs"] = flat[: max(1, min(max_paragraphs, 200))]
        if include_logs:
            logs = await client.request(
                "GET", f"/api/scenario/projects/{scenario_project_id}/log", caller_headers=headers
            )
            out["recent_logs"] = logs[-50:] if isinstance(logs, list) else logs
        return out

    @mcp.tool(title="Create Scenario Project", annotations=CREATE_ACTION)
    async def create_scenario_project(
        title: str | None = None,
        topic: str | None = None,
        niche: str | None = None,
        language: str | None = None,
        length: str | None = None,
        tone: str | None = None,
        model: str | None = None,
        materials: str | None = None,
        brief: bool = True,
        launch_now: bool = False,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Create a new scriptwriting project in `fl-scenario` and optionally start brief generation or full script launch."""
        body: dict[str, Any] = {"brief": brief, "launch_now": launch_now}
        for key, val in (
            ("title", title),
            ("topic", topic),
            ("niche", niche),
            ("language", language),
            ("length", length),
            ("tone", tone),
            ("model", model),
            ("materials", materials),
        ):
            if val is not None:
                body[key] = val
        return await client.request("POST", "/api/scenario/projects", json_body=body, caller_headers=_ctx_headers(ctx))

    @mcp.tool(title="Control Scenario Project", annotations=SAFE_MUTATE)
    async def control_scenario_project(
        scenario_project_id: str,
        action: Literal["brief", "launch", "stop", "resume"],
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Trigger `brief`, `launch`, `stop`, or `resume` on a `fl-scenario` project."""
        return await client.request(
            "POST",
            f"/api/scenario/projects/{scenario_project_id}/{action}",
            json_body={},
            caller_headers=_ctx_headers(ctx),
        )

    # ------------------------------------------------------------------ Stage 2: Prompts (fl-prompts + builtin fl-api)

    @mcp.tool(title="List Prompt Projects & Templates", annotations=READ_ONLY)
    async def list_prompts_overview(
        project_id: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """List prompt generation projects (`fl-prompts` if connected, plus built-in `fl-api` templates & runs)."""
        headers = _ctx_headers(ctx)
        result: dict[str, Any] = {}
        try:
            pg_projects = await client.request(
                "GET",
                "/api/prompts/projects",
                params={"project_id": project_id},
                caller_headers=headers,
            )
            result["fl_prompts_service"] = {"online": True, "count": len(pg_projects), "projects": pg_projects[:40]}
        except PlatformAPIError as exc:
            result["fl_prompts_service"] = {"online": False, "reason": exc.detail}

        try:
            templates, runs = await asyncio.gather(
                client.request("GET", "/api/prompts/templates", caller_headers=headers),
                client.request("GET", "/api/prompts/runs", params={"project_id": project_id}, caller_headers=headers),
            )
            result["builtin_prompts"] = {
                "templates_count": len(templates),
                "templates": templates,
                "runs_count": len(runs),
                "runs": runs[:40],
            }
        except PlatformAPIError:
            pass
        return result

    @mcp.tool(title="Get Prompt Run or Project Details", annotations=READ_ONLY)
    async def get_prompt_run_details(
        run_id: str,
        backend: Literal["auto", "fl_prompts", "builtin"] = "auto",
        offset: int = 0,
        limit: int = 40,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Get stage status and paginated `(img, vid)` prompt pairs from a `fl-prompts` project or built-in `fl-api` prompt run."""
        headers = _ctx_headers(ctx)
        page_size = min(max(limit, 1), 100)
        start = max(offset, 0)

        if backend in ("auto", "fl_prompts"):
            try:
                proj = await client.request("GET", f"/api/prompts/projects/{run_id}", caller_headers=headers)
                rows: list[dict[str, Any]] = []
                summary = proj.get("summary")
                if proj.get("status") in ("running", "done") and proj.get("launched"):
                    try:
                        res = await client.request(
                            "GET", f"/api/prompts/projects/{run_id}/result", caller_headers=headers
                        )
                        rows = res.get("rows", [])
                        summary = res.get("summary") or summary
                    except PlatformAPIError:
                        pass
                return {
                    "backend": "fl_prompts",
                    "project": proj,
                    "summary": summary,
                    "pagination": {"offset": start, "limit": page_size, "total_rows": len(rows)},
                    "rows": rows[start : start + page_size],
                }
            except PlatformAPIError:
                if backend == "fl_prompts":
                    raise

        detail = await client.request("GET", f"/api/prompts/runs/{run_id}", caller_headers=headers)
        all_rows = detail.get("rows", [])
        meta = {k: v for k, v in detail.items() if k != "rows"}
        return {
            "backend": "builtin",
            "run": meta,
            "pagination": {"offset": start, "limit": page_size, "total_rows": len(all_rows)},
            "rows": all_rows[start : start + page_size],
        }

    @mcp.tool(title="Create Built-in Prompt Style Template", annotations=CREATE_ACTION)
    async def create_prompt_template(
        brief_text: str | None = None,
        brief_file_path: str | None = None,
        title: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Extract a reusable `I2VPrompt` style template from a brief (`.txt`/`.docx` path or plain text) in `fl-api`."""
        if bool(brief_text) == bool(brief_file_path):
            raise ValueError("Provide exactly one of `brief_text` or `brief_file_path`.")
        if brief_file_path:
            path = Path(brief_file_path).expanduser().resolve()
            if not path.is_file():
                raise ValueError(f"Brief file not found: {path}")
            filename = path.name
            raw = await asyncio.to_thread(path.read_bytes)
        else:
            assert brief_text is not None
            filename = f"{(title or 'brief').strip()}.txt"
            raw = brief_text.encode("utf-8")

        return await client.request(
            "POST",
            "/api/prompts/templates",
            data={"title": title} if title else None,
            files={"file": (filename, raw)},
            caller_headers=_ctx_headers(ctx),
            timeout=120.0,
        )

    @mcp.tool(title="Start Prompt Generation", annotations=CREATE_ACTION)
    async def start_prompt_generation(
        backend: Literal["auto", "fl_prompts", "builtin"] = "auto",
        scenario_project_id: str | None = None,
        scenario_file_path: str | None = None,
        scenario_text: str | None = None,
        title: str | None = None,
        project_id: str | None = None,
        pair_count: int = 2,
        model: str | None = None,
        # fl-prompts specific:
        niche: str | None = None,
        pg_mode: Literal["regular", "avatar"] = "regular",
        pg_base: Literal["template", "passport"] = "template",
        notes: str | None = None,
        review_brief: bool = False,
        # builtin fl-api specific:
        template_id: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """
        Start generating image+video prompt pairs for a scenario.

        Supports both `fl-prompts` (`backend='fl_prompts'` or `'auto'`) and built-in `fl-api` (`backend='builtin'`, requires `template_id`).
        """
        headers = _ctx_headers(ctx)
        use_fl_prompts = backend == "fl_prompts"
        if backend == "auto" and not template_id:
            probe = await client.probe_optional("/api/prompts/health", caller_headers=headers)
            use_fl_prompts = probe.get("state") == "online"

        if use_fl_prompts:
            script_spec: dict[str, Any]
            if scenario_project_id:
                script_spec = {"source": "project", "id": scenario_project_id}
            elif scenario_file_path:
                path = Path(scenario_file_path).expanduser().resolve()
                if not path.is_file():
                    raise ValueError(f"Scenario file not found: {path}")
                paragraphs = await asyncio.to_thread(parse_scenario, path.name, path.read_bytes())
                script_spec = {"source": "file", "name": path.name, "paragraphs": paragraphs}
            elif scenario_text:
                paragraphs = await asyncio.to_thread(parse_scenario, "scenario.txt", scenario_text.encode("utf-8"))
                script_spec = {"source": "file", "name": f"{title or 'scenario'}.txt", "paragraphs": paragraphs}
            else:
                raise ValueError("Provide `scenario_project_id`, `scenario_file_path`, or `scenario_text`.")

            body: dict[str, Any] = {
                "mode": pg_mode,
                "base": pg_base,
                "pairs": pair_count,
                "review_brief": review_brief,
                "project_id": project_id,
                "script": script_spec,
            }
            for k, v in (("title", title), ("niche", niche), ("notes", notes), ("model", model)):
                if v is not None:
                    body[k] = v
            return await client.request("POST", "/api/prompts/projects", json_body=body, caller_headers=headers)

        # Built-in fl-api prompt runner
        if not template_id:
            raise ValueError("`template_id` is required when using the built-in `fl-api` prompt generator.")
        fname, docx_or_raw = await _scenario_docx_bytes(
            client,
            docx_path=scenario_file_path,
            scenario_text=scenario_text,
            scenario_project_id=scenario_project_id,
            default_title=title or "scenario",
            caller_headers=headers,
        )
        form_data: dict[str, Any] = {
            "template_id": template_id,
            "model": model or "gemini-2.5-flash",
            "pair_count": str(pair_count),
        }
        if project_id:
            form_data["project_id"] = project_id
        return await client.request(
            "POST",
            "/api/prompts/runs",
            data=form_data,
            files={"file": (fname, docx_or_raw)},
            caller_headers=headers,
        )

    @mcp.tool(title="Control fl-prompts Project", annotations=SAFE_MUTATE)
    async def control_prompts_project(
        prompts_project_id: str,
        action: Literal["launch", "stop", "resume", "rebrief"],
        notes: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Run `launch` (after brief review), `stop`, `resume`, or `rebrief` on a `fl-prompts` project."""
        body = {"notes": notes} if action == "rebrief" and notes is not None else {}
        return await client.request(
            "POST",
            f"/api/prompts/projects/{prompts_project_id}/{action}",
            json_body=body,
            caller_headers=_ctx_headers(ctx),
        )

    @mcp.tool(title="Handoff Prompts To Generation Job", annotations=CREATE_ACTION)
    async def handoff_prompts_to_job(
        prompt_run_id: str,
        mode: Mode = Mode.I2V,
        backend: Literal["auto", "fl_prompts", "builtin"] = "auto",
        project_id: str | None = None,
        avatar_id: str | None = None,
        avatar_key: str | None = None,
        avatar_keys: list[str] | None = None,
        static_image_key: str | None = None,
        end_image_key: str | None = None,
        title: str | None = None,
        strategy: Strategy | None = None,
        duration: Literal[4, 6, 8, 10] | None = None,
        aspect_ratio: Literal["16:9", "9:16", "1:1"] | None = None,
        video_model_key: str | None = None,
        apply_project_style: bool = True,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """
        Send finished prompt pairs from `fl-prompts` or built-in `fl-api` directly into `JobQueue` (like clicking «В производство»).

        Automatically applies `avatar.appearance` (for `i2i2v`) and `project.style` suffix, without downloading `.xlsx` files.
        Supports `duration` (`4`, `6`, `8`, or `10` sec depending on `mode`) and `aspect_ratio` (`"16:9"`, `"9:16"`, or `"1:1"` for `img`).
        """
        headers = _ctx_headers(ctx)
        raw_rows: list[dict[str, Any]] = []
        inferred_title: str | None = None
        inferred_project_id: str | None = project_id

        loaded_from_pg = False
        if backend in ("auto", "fl_prompts"):
            try:
                pg_proj = await client.request("GET", f"/api/prompts/projects/{prompt_run_id}", caller_headers=headers)
                pg_res = await client.request(
                    "GET", f"/api/prompts/projects/{prompt_run_id}/result", caller_headers=headers
                )
                inferred_title = pg_proj.get("title")
                inferred_project_id = inferred_project_id or pg_proj.get("project_id")
                raw_rows = [
                    {"name": str(r["number"]), "prompt": r.get("vid"), "image_prompt": r.get("img")}
                    for r in pg_res.get("rows", [])
                    if r.get("img") or r.get("vid")
                ]
                loaded_from_pg = True
            except PlatformAPIError:
                if backend == "fl_prompts":
                    raise

        if not loaded_from_pg:
            run_detail = await client.request("GET", f"/api/prompts/runs/{prompt_run_id}", caller_headers=headers)
            inferred_title = run_detail.get("scenario_name")
            inferred_project_id = inferred_project_id or run_detail.get("project_id")
            raw_rows = [
                {
                    "name": f"{r['paragraph_number']}.{r['pair_number']}",
                    "prompt": r.get("vid"),
                    "image_prompt": r.get("img"),
                }
                for r in run_detail.get("rows", [])
                if r.get("img") or r.get("vid")
            ]

        if not raw_rows:
            raise ValueError(f"Prompt source {prompt_run_id} has no generated prompt rows yet.")

        spec = MODES[mode]
        resolved_avatar_key, resolved_avatar_keys, avatar_appearance = await resolve_avatar_refs(
            client, avatar_id, avatar_key, avatar_keys=avatar_keys, caller_headers=headers
        )
        style = await resolve_project_style(client, inferred_project_id, headers) if apply_project_style else ""
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
            "title": title or inferred_title,
            "project_id": inferred_project_id,
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

    # ------------------------------------------------------------------ Stage 3: Infographics (motion-aign + Remotion)

    @mcp.tool(title="List Infographics Runs", annotations=READ_ONLY)
    async def list_infographics_runs(
        project_id: str | None = None,
        ctx: Context | None = None,
    ) -> list[dict[str, Any]]:
        """List animated card (people / places / artifacts) runs and their searching/rendering progress."""
        return await client.request(
            "GET",
            "/api/infographics/runs",
            params={"project_id": project_id},
            caller_headers=_ctx_headers(ctx),
        )

    @mcp.tool(title="Get Infographics Run Details", annotations=READ_ONLY)
    async def get_infographics_run(
        run_id: str,
        include_logs: bool = True,
        card_limit: int = 40,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Inspect an infographics run's state (`queued`/`searching`/`rendering`/`done`/`failed`), rendered cards, and subprocess logs."""
        headers = _ctx_headers(ctx)
        detail = await client.request("GET", f"/api/infographics/runs/{run_id}", caller_headers=headers)
        cards = detail.get("cards", [])
        out: dict[str, Any] = {
            "run": {k: v for k, v in detail.items() if k != "cards"},
            "total_cards": len(cards),
            "cards": cards[: max(1, min(card_limit, 150))],
        }
        if include_logs:
            try:
                log_resp = await client.request(
                    "GET",
                    f"/api/infographics/runs/{run_id}/log",
                    params={"lines": 40},
                    caller_headers=headers,
                )
                out["recent_logs"] = log_resp.get("lines", [])
            except PlatformAPIError:
                pass
        return out

    @mcp.tool(title="Start Infographics Run", annotations=CREATE_ACTION)
    async def start_infographics_run(
        categories: list[Literal["people", "places", "artifacts"]] | None = None,
        docx_path: str | None = None,
        scenario_text: str | None = None,
        scenario_project_id: str | None = None,
        title: str = "scenario",
        one_per_person: bool = False,
        project_id: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """
        Launch Wikidata/Wikimedia subject discovery + Remotion card rendering for a scenario.

        Accepts a local `.docx`/`.txt` (`docx_path`), raw `scenario_text` (auto-converted to `.docx`), or a `fl-scenario` `scenario_project_id`.
        """
        headers = _ctx_headers(ctx)
        chosen = categories or ["people"]
        fname, docx_bytes = await _scenario_docx_bytes(
            client,
            docx_path=docx_path,
            scenario_text=scenario_text,
            scenario_project_id=scenario_project_id,
            default_title=title,
            caller_headers=headers,
        )
        form_data: dict[str, Any] = {
            "categories": ",".join(chosen),
            "one_per_person": str(one_per_person).lower(),
        }
        if project_id:
            form_data["project_id"] = project_id
        return await client.request(
            "POST",
            "/api/infographics/runs",
            data=form_data,
            files={"file": (fname, docx_bytes)},
            caller_headers=headers,
        )

    @mcp.tool(title="Retry or Delete Infographics Run", annotations=SAFE_MUTATE)
    async def control_infographics_run(
        run_id: str,
        action: Literal["retry", "delete"],
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Retry a failed infographics run (resumes from cached scenario/clips) or delete a run."""
        headers = _ctx_headers(ctx)
        if action == "delete":
            return await client.request("DELETE", f"/api/infographics/runs/{run_id}", caller_headers=headers)
        return await client.request("POST", f"/api/infographics/runs/{run_id}/retry", caller_headers=headers)

    # ------------------------------------------------------------------ Stage 4: Voice & Translation (fl-voice_translator)

    @mcp.tool(title="Get Voice & Translation Overview", annotations=READ_ONLY)
    async def get_voice_overview(ctx: Context | None = None) -> dict[str, Any]:
        """Fetch queue status (gemini/claude/voice/mix slots), profiles, models, and languages from `fl-voice_translator`."""
        headers = _ctx_headers(ctx)
        queue, models, profiles, languages = await asyncio.gather(
            client.request("GET", "/api/voice/queue", caller_headers=headers),
            client.request("GET", "/api/voice/models", caller_headers=headers),
            client.request("GET", "/api/voice/profiles", caller_headers=headers),
            client.request("GET", "/api/voice/languages", caller_headers=headers),
        )
        return {"queue": queue, "models": models, "profiles": profiles, "languages": languages}

    @mcp.tool(title="List Voice Jobs", annotations=READ_ONLY)
    async def list_voice_jobs(
        status: str | None = None,
        project_id: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """List translation, Fish TTS voiceover, and audio mixing jobs."""
        jobs = await client.request(
            "GET",
            "/api/voice/jobs",
            params={"status": status, "project_id": project_id},
            caller_headers=_ctx_headers(ctx),
        )
        return {"count": len(jobs), "jobs": jobs[:50]}

    @mcp.tool(title="Get Voice Job Details", annotations=READ_ONLY)
    async def get_voice_job(job_id: int, ctx: Context | None = None) -> dict[str, Any]:
        """Get per-language translation, Fish voiceover, and audio mixing stage progress and output files for a voice job."""
        return await client.request("GET", f"/api/voice/jobs/{job_id}", caller_headers=_ctx_headers(ctx))

    @mcp.tool(title="Start Voice & Translation Job", annotations=CREATE_ACTION)
    async def start_voice_job(
        targets: list[str],
        profile_id: str,
        mode: Literal["all", "translate", "voice", "mix"] = "all",
        source_lang: str = "ru",
        model: str = "gemini-2.5-pro",
        voice_model: Literal["s2.1", "drama-3"] = "s2.1",
        script_id: str | None = None,
        docx_path: str | None = None,
        scenario_text: str | None = None,
        scenario_project_id: str | None = None,
        title: str | None = None,
        project_id: str | None = None,
        voices: dict[str, str] | None = None,
        carry_mood: bool = True,
        split_by_language: bool = False,
        turbo: bool = False,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Upload a script (if `script_id` is not provided) and launch translation / Fish TTS voiceover / mixing in `fl-voice_translator`."""
        headers = _ctx_headers(ctx)
        resolved_script_id = script_id
        if not resolved_script_id:
            fname, docx_bytes = await _scenario_docx_bytes(
                client,
                docx_path=docx_path,
                scenario_text=scenario_text,
                scenario_project_id=scenario_project_id,
                default_title=title or "script",
                caller_headers=headers,
            )
            uploaded = await client.request(
                "POST",
                "/api/voice/scripts",
                data={"title": title} if title else None,
                files={"file": (fname, docx_bytes)},
                caller_headers=headers,
            )
            resolved_script_id = uploaded["script_id"]

        body: dict[str, Any] = {
            "script_id": resolved_script_id,
            "title": title,
            "project_id": project_id,
            "mode": mode,
            "source_lang": source_lang,
            "targets": targets,
            "profile_id": profile_id,
            "voices": voices,
            "model": model,
            "voice_model": voice_model,
            "carry_mood": carry_mood,
            "split_by_language": split_by_language,
            "turbo": turbo,
        }
        clean_body = {k: v for k, v in body.items() if v is not None}
        return await client.request("POST", "/api/voice/jobs", json_body=clean_body, caller_headers=headers)

    @mcp.tool(title="Control Voice Job", annotations=SAFE_MUTATE)
    async def control_voice_job(
        job_id: int,
        action: Literal["stop", "resume"],
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Stop or resume a `fl-voice_translator` job."""
        return await client.request(
            "POST", f"/api/voice/jobs/{job_id}/{action}", json_body={}, caller_headers=_ctx_headers(ctx)
        )

    # ------------------------------------------------------------------ Stage 5: Footage Search (fl-footage)

    @mcp.tool(title="List Footage Projects", annotations=READ_ONLY)
    async def list_footage_projects(ctx: Context | None = None) -> dict[str, Any]:
        """List archival/YouTube/film/stock footage search projects (`plan -> search -> extra -> cut -> done`)."""
        projects = await client.request("GET", "/api/footage/projects", caller_headers=_ctx_headers(ctx))
        return {"count": len(projects), "projects": projects[:50]}

    @mcp.tool(title="Get Footage Project Details", annotations=READ_ONLY)
    async def get_footage_project(
        footage_project_id: str,
        include_logs: bool = False,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Get stage progress, source group stats, and optional recent logs for a `fl-footage` project."""
        headers = _ctx_headers(ctx)
        proj = await client.request("GET", f"/api/footage/projects/{footage_project_id}", caller_headers=headers)
        out: dict[str, Any] = {"project": proj}
        if include_logs:
            logs = await client.request("GET", f"/api/footage/projects/{footage_project_id}/log", caller_headers=headers)
            out["recent_logs"] = logs[-50:] if isinstance(logs, list) else logs
        return out

    @mcp.tool(title="Start Footage Search Project", annotations=CREATE_ACTION)
    async def start_footage_project(
        title: str,
        voice_job_id: int | None = None,
        script_path: str | None = None,
        groups: list[Literal["archive", "youtube", "films", "stock"]] | None = None,
        aspect_format: Literal["16:9", "9:16"] = "16:9",
        plan_only: bool = False,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Launch a `fl-footage` search project from a finished `voice_job_id` or a local script file."""
        import json

        if bool(voice_job_id is not None) == bool(script_path):
            raise ValueError("Provide exactly one of `voice_job_id` or `script_path`.")

        payload: dict[str, Any] = {
            "title": title,
            "source": "voice" if voice_job_id is not None else "file",
            "options": {
                "groups": groups or ["archive", "youtube", "stock"],
                "format": aspect_format,
                "plan_only": plan_only,
            },
        }
        files = None
        if voice_job_id is not None:
            payload["voice_job"] = voice_job_id
        else:
            assert script_path is not None
            p = Path(script_path).expanduser().resolve()
            if not p.is_file():
                raise ValueError(f"Script file not found: {p}")
            files = {"script": (p.name, await asyncio.to_thread(p.read_bytes))}

        return await client.request(
            "POST",
            "/api/footage/projects",
            data={"data": json.dumps(payload, ensure_ascii=False)},
            files=files,
            caller_headers=_ctx_headers(ctx),
        )

    @mcp.tool(title="Control Footage Project", annotations=SAFE_MUTATE)
    async def control_footage_project(
        footage_project_id: str,
        action: Literal["stop", "resume"],
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Stop or resume a `fl-footage` project."""
        return await client.request(
            "POST",
            f"/api/footage/projects/{footage_project_id}/{action}",
            json_body={},
            caller_headers=_ctx_headers(ctx),
        )
