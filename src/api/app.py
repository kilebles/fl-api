"""FastAPI app: the web API over JobQueue. Run with `uv run python scripts/serve.py`."""

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from loguru import logger

from src.accounts.repository import AccountRepository
from src.api.prompts import router as prompts_router
from src.api.routes import router
from src.core.db import Database
from src.core.settings import Settings
from src.jobs.queue import InputNotFoundError, JobNotFoundError, JobQueue
from src.jobs.repository import JobRepository
from src.jobs.runner import JobRunner
from src.prompts.llm import LLMClient, LLMNotConfiguredError
from src.prompts.service import NotFoundError, PromptService
from src.storage.objects import LocalObjectStore, ObjectStore, make_object_store


def create_app(
    settings: Settings,
    objects: ObjectStore | None = None,
    runner: JobRunner | None = None,
    llm: LLMClient | None = None,
) -> FastAPI:
    """Build the app; `objects`, `runner` and `llm` can be replaced (tests)."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        accounts = AccountRepository(settings.accounts_dir)
        store = objects or make_object_store(settings)
        try:
            await store.ensure_bucket()
        except Exception as e:
            logger.warning(f"[api] storage is not reachable: {type(e).__name__}: {e}")
        db = Database(settings.database_url)
        await db.init()
        queue = JobQueue(runner or JobRunner(settings, accounts), JobRepository(db), store, settings.work_dir)
        prompts = PromptService(
            db,
            llm or LLMClient(settings.google_api_key, settings.openai_api_key),
            settings.prompt_template_model,
            parallel_runs=settings.prompt_parallel_runs,
        )
        app.state.accounts = accounts
        app.state.objects = store
        app.state.queue = queue
        app.state.prompts = prompts
        await queue.start()
        await prompts.start()
        try:
            yield
        finally:
            await prompts.stop()
            await queue.stop()
            await db.close()

    def check_token(request: Request) -> None:
        if not settings.api_token:
            return
        header = request.headers.get("authorization", "")
        token = header.removeprefix("Bearer ").strip() or request.query_params.get("token", "")
        if not secrets.compare_digest(token, settings.api_token):
            raise HTTPException(401, "invalid token")

    app = FastAPI(title="fl-api", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router, dependencies=[Depends(check_token)])
    app.include_router(prompts_router, dependencies=[Depends(check_token)])

    @app.get("/files/{key:path}", include_in_schema=False)
    async def local_file(key: str, request: Request, download: str | None = None) -> FileResponse:
        """Files of the local storage (STORAGE=local); supports range requests for video seeking."""
        store = request.app.state.objects
        if not isinstance(store, LocalObjectStore):
            raise HTTPException(404)
        try:
            path = store.path(key)
        except ValueError:
            raise HTTPException(404) from None
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, filename=download, content_disposition_type="attachment" if download else "inline")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.exception_handler(JobNotFoundError)
    async def _not_found(request: Request, exc: JobNotFoundError) -> JSONResponse:
        return JSONResponse({"detail": f"job {exc} not found"}, status_code=404)

    @app.exception_handler(NotFoundError)
    async def _prompt_not_found(request: Request, exc: NotFoundError) -> JSONResponse:
        return JSONResponse({"detail": f"{exc} not found"}, status_code=404)

    @app.exception_handler(LLMNotConfiguredError)
    async def _llm_missing(request: Request, exc: LLMNotConfiguredError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=503)

    @app.exception_handler(InputNotFoundError)
    async def _bad_input(request: Request, exc: InputNotFoundError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=422)

    return app
