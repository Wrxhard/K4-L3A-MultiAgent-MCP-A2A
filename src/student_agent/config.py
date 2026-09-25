from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

TEAM_KEY_PATTERN = re.compile(r"^sk-team-[A-Za-z0-9_-]{16,128}$")


@dataclass(frozen=True)
class Settings:
    competition_api_url: str
    team_api_key: str
    mcp_endpoint: str
    model_api_url: str
    model_api_key: str
    qwen_model_id: str
    root: Path

    @classmethod
    def load(cls, root: Path | None = None) -> Settings:
        resolved_root = (root or Path.cwd()).resolve()
        load_dotenv(resolved_root / ".env")
        api_url = os.getenv("COMPETITION_API_URL", "").strip().rstrip("/")
        team_key = os.getenv("COMPETITION_TEAM_API_KEY", "").strip()
        mcp_endpoint = os.getenv("MCP_ENDPOINT", "").strip()
        model_api_url = os.getenv("MODEL_API_URL", "").strip().rstrip("/")
        model_api_key = os.getenv("MODEL_API_KEY", "").strip()
        qwen_model_id = os.getenv("QWEN_MODEL_ID", "Qwen/Qwen3-4B").strip()
        errors: list[str] = []
        if not api_url.startswith(("http://", "https://")):
            errors.append("COMPETITION_API_URL must be an absolute HTTP(S) URL")
        if not TEAM_KEY_PATTERN.fullmatch(team_key):
            errors.append("COMPETITION_TEAM_API_KEY must use the sk-team-... format")
        if not mcp_endpoint.startswith(("http://", "https://")):
            errors.append("MCP_ENDPOINT must be an absolute HTTP(S) URL")
        if model_api_url and not model_api_url.startswith(("http://", "https://")):
            errors.append("MODEL_API_URL must be empty or an absolute HTTP(S) URL")
        if not qwen_model_id:
            errors.append("QWEN_MODEL_ID must be a non-empty string")
        if errors:
            raise ValueError("; ".join(errors))
        return cls(
            api_url,
            team_key,
            mcp_endpoint,
            model_api_url,
            model_api_key,
            qwen_model_id,
            resolved_root,
        )
