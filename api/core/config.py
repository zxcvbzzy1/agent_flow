from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class APISettings:
    app_name: str = "Agent Flow API"
    mongo_url: str = os.getenv("AGENT_FLOW_MONGO_URL", "mongodb://localhost:27017/")
    mongo_db: str = os.getenv("AGENT_FLOW_MONGO_DB", "agent_flow")
    cors_origins: tuple[str, ...] = tuple(
        origin.strip()
        for origin in os.getenv(
            "AGENT_FLOW_CORS_ORIGINS",
            "http://localhost:5173,http://127.0.0.1:5173",
        ).split(",")
        if origin.strip()
    )


settings = APISettings()

