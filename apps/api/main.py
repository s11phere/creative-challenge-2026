"""API entry point for the Agent Knowledge Repository."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Manage application lifecycle."""
    yield


app = FastAPI(
    title="Agent Knowledge Repository",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/api/v1/health/live")
async def live() -> dict[str, str]:
    return {"status": "alive"}


@app.get("/api/v1/health/ready")
async def ready() -> dict[str, str]:
    return {"status": "ready"}
