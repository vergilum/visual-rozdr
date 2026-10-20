from functools import lru_cache
from pathlib import Path
import shutil
import subprocess
import sys
import platform

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Visual Pozdr"
    host: str = "127.0.0.1"
    port: int = 8000
    data_dir: Path = Path("data")
    database_path: Path | None = None
    frontend_dist_dir: Path | None = None
    min_free_disk_mb: int = 1024
    allowed_origins: list[str] = Field(default_factory=lambda: ["http://127.0.0.1:8000", "http://localhost:8000"])
    local_session_secret: str = "dev-local-session-change-me"
    telegram_bot_token: str | None = None
    telegram_bot_username: str | None = None
    video_worker_concurrency: int = 1
    telegram_download_timeout_sec: int = 60
    ffprobe_timeout_sec: int = 20
    ffmpeg_timeout_sec: int = 120
    ffmpeg_path: Path | None = None
    ffprobe_path: Path | None = None
    video_processing_retries: int = 2
    video_processing_retry_delay_sec: int = 3
    video_worker_poll_interval_sec: float = 2.0
    screen_heartbeat_timeout_sec: int = 10
    screen_load_timeout_sec: int = 12

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def resolved_data_dir(self) -> Path:
        return self.data_dir.resolve()

    @property
    def resolved_database_path(self) -> Path:
        if self.database_path is not None:
            return self.database_path.resolve()
        return self.resolved_data_dir / "app.sqlite3"

    @property
    def resolved_frontend_dist_dir(self) -> Path:
        if self.frontend_dist_dir is not None:
            return self.frontend_dist_dir.resolve()
        return (Path(__file__).resolve().parents[2] / "frontend" / "dist").resolve()

    @property
    def ffmpeg_binary(self) -> str | None:
        return resolve_tool_binary(self.ffmpeg_path, "ffmpeg")

    @property
    def ffprobe_binary(self) -> str | None:
        return resolve_tool_binary(self.ffprobe_path, "ffprobe")

    @property
    def media_tools_diagnostics(self) -> dict[str, dict[str, str | None]]:
        return {
            "ffmpeg": inspect_tool(self.ffmpeg_binary),
            "ffprobe": inspect_tool(self.ffprobe_binary),
        }


def resolve_tool_binary(configured_path: Path | None, name: str) -> str | None:
    candidates: list[Path] = []
    if configured_path is not None:
        candidates.append(configured_path)
    runtime = Path(__file__).resolve().parents[2] / "runtime" / "ffmpeg"
    if sys.platform == "darwin":
        arch = "arm64" if platform.machine() == "arm64" else "x64"
        runtime = runtime / f"darwin-{arch}"
    candidates.append(runtime / (f"{name}.exe" if sys.platform == "win32" else name))
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate.resolve())
    return shutil.which(name)


@lru_cache
def inspect_tool(binary: str | None) -> dict[str, str | None]:
    if binary is None:
        return {"status": "missing", "path": None, "version": None}
    try:
        completed = subprocess.run([binary, "-version"], capture_output=True, check=False, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return {"status": "missing", "path": binary, "version": None}
    if completed.returncode != 0:
        return {"status": "error", "path": binary, "version": None}
    first_line = completed.stdout.splitlines()[0] if completed.stdout else ""
    return {"status": "available", "path": binary, "version": first_line or None}


@lru_cache
def get_settings() -> Settings:
    return Settings()
