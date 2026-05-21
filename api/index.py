from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import uvicorn


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from api.agents.router import router as agents_router  # noqa: E402
from api.contexts.router import router as contexts_router  # noqa: E402
from api.conversations.router import router as conversations_router  # noqa: E402
from api.core.config import settings  # noqa: E402
from api.core.dependencies import get_container  # noqa: E402
from api.runs.router import router as runs_router  # noqa: E402
from api.tools.router import router as tools_router  # noqa: E402


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_container()
    yield


def create_app() -> FastAPI:
    app = FastAPI(title=settings.app_name, lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(tools_router)
    app.include_router(contexts_router)
    app.include_router(agents_router)
    app.include_router(runs_router)
    app.include_router(conversations_router)

    @app.get("/health")
    async def health():
        container = get_container()
        return {
            "status": "ok",
            "mongo": "memory" if container.store.using_memory else "mongodb",
        }

    return app


app = create_app()


if __name__ == "__main__":
    uvicorn.run(
        "index:app",   # index.py 文件名
        host="0.0.0.0",
        port=8000,
        reload=True,
    )