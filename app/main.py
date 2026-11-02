"""FastAPI application factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import auth, schemas
from app.config import Settings, get_settings
from app.db import get_session
from app.models import User
from app.routers import clouds, jobs, shares, users
from app.storage import get_storage


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    get_storage().ensure_dirs()
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="pclapp-service",
        version="0.1.0",
        description="Upload point clouds, queue processing jobs, fetch results.",
        lifespan=_lifespan,
    )
    app.include_router(users.router)
    app.include_router(clouds.router)
    app.include_router(jobs.router)
    app.include_router(shares.router)

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        """A 60-line page that lists clouds and jobs and follows running jobs over SSE."""
        return FileResponse(Path(__file__).parent / "static" / "index.html")

    @app.get("/me", response_model=schemas.UserOut, tags=["users"])
    def me(user: auth.CurrentUser) -> User:
        """Who does this key belong to? Also the cheapest way to check a key works."""
        return user

    @app.get("/healthz", response_model=schemas.Health, tags=["ops"])
    def healthz(
        session: Annotated[Session, Depends(get_session)],
        settings: Annotated[Settings, Depends(get_settings)],
    ) -> JSONResponse:
        db = "ok"
        try:
            session.execute(text("SELECT 1"))
        except Exception:  # noqa: BLE001 - any failure is "not ok" for a health check
            db = "error"
        data_dir = "ok" if settings.data_dir.is_dir() else "missing"
        body = schemas.Health(db=db, data_dir=data_dir)
        code = 200 if db == "ok" and data_dir == "ok" else 503
        return JSONResponse(body.model_dump(), status_code=code)

    return app


app = create_app()
