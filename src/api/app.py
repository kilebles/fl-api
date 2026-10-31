"""FastAPI app: the web API over JobQueue. Run with `uv run python scripts/serve.py`."""

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from loguru import logger
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.accounts.repository import AccountRepository
from src.api.auth import (
    admin_router,
    auth_router,
    clear_session_cookie,
    get_principal,
    set_session_cookie,
)
from src.api.infographics import router as infographics_router
from src.api.library import router as library_router
from src.api.prompts import router as prompts_router
from src.api.routes import router
from src.auth.mailer import MailDeliveryError, ResendMailer
from src.auth.service import (
    AccountDisabledError,
    AuthError,
    AuthService,
    EmailAlreadyExistsError,
    InvalidCredentialsError,
    RateLimitedError,
    SessionNotFoundError,
    UserNotFoundError,
)
from src.avatars.service import AvatarNotFoundError, AvatarService
from src.core.db import Database
from src.core.settings import Settings
from src.infographics.service import InfographicsService
from src.infographics.service import NotFoundError as InfographicNotFoundError
from src.jobs.queue import InputNotFoundError, JobNotFoundError, JobQueue
from src.jobs.repository import JobRepository
from src.jobs.runner import JobRunner
from src.projects.service import ProjectNotFoundError, ProjectService
from src.prompts.llm import LLMClient, LLMNotConfiguredError
from src.prompts.service import NotFoundError, PromptService
from src.storage.objects import LocalObjectStore, ObjectStore, make_object_store


def create_app(
    settings: Settings,
    objects: ObjectStore | None = None,
    runner: JobRunner | None = None,
    llm: LLMClient | None = None,
    mailer: ResendMailer | None = None,
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
        auth = AuthService(
            db,
            session_days=settings.auth_session_days,
            admin_emails=settings.admin_emails,
            first_user_admin=settings.first_user_admin,
        )
        queue = JobQueue(runner or JobRunner(settings, accounts), JobRepository(db), store, settings.work_dir)
        llm_client = llm or LLMClient(settings.google_api_key, settings.openai_api_key)
        prompts = PromptService(
            db,
            llm_client,
            settings.prompt_template_model,
            parallel_runs=settings.prompt_parallel_runs,
        )
        app.state.settings = settings
        app.state.oauth_secret = settings.auth_secret or secrets.token_hex(32)
        app.state.auth = auth
        app.state.mailer = mailer or ResendMailer(settings.resend_api_key, settings.resend_from)
        app.state.accounts = accounts
        app.state.objects = store
        app.state.queue = queue
        app.state.prompts = prompts
        app.state.infographics = infographics = InfographicsService(db, store, settings)
        app.state.projects = ProjectService(db)
        app.state.avatars = AvatarService(db)
        await queue.start()
        await prompts.start()
        await infographics.start()
        try:
            yield
        finally:
            await infographics.stop()
            await prompts.stop()
            await queue.stop()
            await db.close()

    app = FastAPI(title="fl-api", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def sync_session_cookie(request: Request, call_next) -> Response:
        response: Response = await call_next(request)
        renew = getattr(request.state, "renew_session_token", None)
        if renew:
            set_session_cookie(response, request, settings, renew)
        elif getattr(request.state, "clear_auth_cookie", False):
            clear_session_cookie(response, request, settings)
        return response

    app.include_router(auth_router)
    app.include_router(admin_router)
    app.include_router(router, dependencies=[Depends(get_principal)])
    app.include_router(prompts_router, dependencies=[Depends(get_principal)])
    app.include_router(infographics_router, dependencies=[Depends(get_principal)])
    app.include_router(library_router, dependencies=[Depends(get_principal)])

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

    @app.exception_handler(StarletteHTTPException)
    async def _http_exc(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        resp = JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
        if getattr(request.state, "clear_auth_cookie", False):
            clear_session_cookie(resp, request, settings)
        return resp

    @app.exception_handler(InvalidCredentialsError)
    async def _invalid_creds(request: Request, exc: InvalidCredentialsError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=401)

    @app.exception_handler(AccountDisabledError)
    async def _disabled(request: Request, exc: AccountDisabledError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=403)

    @app.exception_handler(EmailAlreadyExistsError)
    async def _email_exists(request: Request, exc: EmailAlreadyExistsError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(RateLimitedError)
    async def _rate_limited(request: Request, exc: RateLimitedError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=429)

    @app.exception_handler(MailDeliveryError)
    async def _mail_error(request: Request, exc: MailDeliveryError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=502)

    @app.exception_handler(AuthError)
    async def _auth_error(request: Request, exc: AuthError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(JobNotFoundError)
    async def _not_found(request: Request, exc: JobNotFoundError) -> JSONResponse:
        return JSONResponse({"detail": f"job {exc} not found"}, status_code=404)

    @app.exception_handler(NotFoundError)
    async def _prompt_not_found(request: Request, exc: NotFoundError) -> JSONResponse:
        return JSONResponse({"detail": f"{exc} not found"}, status_code=404)

    @app.exception_handler(InfographicNotFoundError)
    async def _infographic_not_found(request: Request, exc: InfographicNotFoundError) -> JSONResponse:
        return JSONResponse({"detail": f"{exc} not found"}, status_code=404)

    @app.exception_handler(ProjectNotFoundError)
    @app.exception_handler(AvatarNotFoundError)
    @app.exception_handler(SessionNotFoundError)
    @app.exception_handler(UserNotFoundError)
    async def _library_not_found(request: Request, exc: LookupError) -> JSONResponse:
        return JSONResponse({"detail": f"{exc} not found"}, status_code=404)

    @app.exception_handler(LLMNotConfiguredError)
    async def _llm_missing(request: Request, exc: LLMNotConfiguredError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=503)

    @app.exception_handler(InputNotFoundError)
    async def _bad_input(request: Request, exc: InputNotFoundError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=422)

    return app
