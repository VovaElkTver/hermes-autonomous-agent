"""Minimal configuration for the rescue core — one flat .env read, no yaml."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _read_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


@dataclass
class RescueConfig:
    homeserver: str
    access_token: str
    home_room: str
    allowed_user: str
    main_unit: str = "hermes-gateway.service"
    model_base: str = "http://192.168.9.100:8001/v1"
    model_fallback: str = "http://192.168.9.100:8012/v1"
    model_id: str = "Inferact/Qwen3.8-27B-NVFP4"
    hermes_home: Path = field(default_factory=lambda: Path.home() / ".hermes")
    rescue_home: Path = field(default_factory=lambda: Path.home() / ".hermes-rescue")
    releases_dir: Path = field(default_factory=lambda: Path.home() / "hermes-releases")
    current_link: Path = field(default_factory=lambda: Path.home() / "hermes-live")
    repo_dir: Path = field(default_factory=lambda: Path.home() / "hermes-agent")
    observe_after_action_s: int = 120
    dry_run: bool = False

    @classmethod
    def load(cls) -> RescueConfig:
        env = _read_env_file(Path.home() / ".hermes" / ".env")
        get = lambda k, d="": env.get(k, os.environ.get(k, d))
        allow = [u.strip() for u in get("MATRIX_ALLOWED_USERS", "@vovak:mx.elk.mobi").split(",") if u.strip()]
        return cls(
            homeserver=get("MATRIX_HOMESERVER", "https://mx.elk.mobi"),
            access_token=get("MATRIX_ACCESS_TOKEN"),
            home_room=get("MATRIX_HOME_ROOM"),
            allowed_user=allow[0] if allow else "@vovak:mx.elk.mobi",
            main_unit=get("HERMES_RESCUE_UNIT", "hermes-gateway.service"),
            model_base=get("HERMES_RESCUE_MODEL_BASE", cls.model_base),
            model_fallback=get("HERMES_RESCUE_MODEL_FALLBACK", cls.model_fallback),
            model_id=get("HERMES_RESCUE_MODEL_ID", cls.model_id),
            dry_run=get("HERMES_RESCUE_DRY_RUN", "0") in {"1", "true", "yes"},
        )
