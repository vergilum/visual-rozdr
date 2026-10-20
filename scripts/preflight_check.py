from __future__ import annotations

import shutil
import socket
import subprocess
import sys
import platform
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def port_is_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) != 0


def resolve_media_tool(root: Path, env: dict[str, str], name: str) -> Path | None:
    configured = env.get(f"{name.upper()}_PATH", "")
    runtime = root / "runtime" / "ffmpeg"
    if sys.platform == "darwin":
        runtime /= "darwin-arm64" if platform.machine() == "arm64" else "darwin-x64"
    candidates = [runtime / (f"{name}.exe" if sys.platform == "win32" else name)]
    if configured:
        configured_path = Path(configured)
        candidates.insert(0, configured_path if configured_path.is_absolute() else root / configured_path)
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    found = shutil.which(name)
    return Path(found).resolve() if found else None


def tool_version(path: Path) -> str | None:
    try:
        result = subprocess.run([str(path), "-version"], capture_output=True, check=False, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout:
        return None
    return result.stdout.splitlines()[0]


def validate(root: Path = ROOT, check_port: bool = True) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    env_path = root / ".env"
    if not env_path.exists():
        errors.append(".env was not found. Create it from .env.example and fill the settings.")
        return errors, warnings

    env = parse_env(env_path)
    host = env.get("HOST", "127.0.0.1") or "127.0.0.1"
    port_raw = env.get("PORT", "8000") or "8000"
    data_dir = root / (env.get("DATA_DIR", "data") or "data")
    min_free_raw = env.get("MIN_FREE_DISK_MB", "1024") or "1024"

    if host != "127.0.0.1":
        errors.append("HOST must be 127.0.0.1 for the local first version.")

    try:
        port = int(port_raw)
        if port < 1 or port > 65535:
            raise ValueError
    except ValueError:
        errors.append("PORT must be a number from 1 to 65535.")
        port = 8000

    if check_port and not errors and not port_is_free(host, port):
        errors.append(f"Port {port} is busy. Stop the old server or change PORT in .env.")

    try:
        min_free_mb = int(min_free_raw)
        if min_free_mb < 100:
            warnings.append("MIN_FREE_DISK_MB is below 100 MB. For an event, keep at least 1024.")
    except ValueError:
        errors.append("MIN_FREE_DISK_MB must be a number.")
        min_free_mb = 1024

    data_dir.mkdir(parents=True, exist_ok=True)
    try:
        free_mb = shutil.disk_usage(data_dir).free // (1024 * 1024)
        if free_mb < min_free_mb:
            errors.append(f"Not enough free disk space: {free_mb} MB available, {min_free_mb} MB required.")
    except OSError as exc:
        errors.append(f"Could not check free disk space in DATA_DIR: {exc}")

    if not (root / "frontend" / "dist" / "index.html").exists():
        errors.append("Frontend is not built. Run: cd frontend && npm run build")

    if not env.get("TELEGRAM_BOT_TOKEN"):
        warnings.append("TELEGRAM_BOT_TOKEN is empty: server will start, but Telegram reception will not work.")
    if not env.get("TELEGRAM_BOT_USERNAME"):
        warnings.append("TELEGRAM_BOT_USERNAME is empty: Telegram QR link will not be generated.")
    for tool in ["ffmpeg", "ffprobe"]:
        path = resolve_media_tool(root, env, tool)
        version = tool_version(path) if path else None
        if path is None or version is None:
            warnings.append(f"{tool} is unavailable. Expected runtime/ffmpeg/{tool}.exe for desktop or {tool} in PATH for development.")
        else:
            warnings.append(f"{tool}: {version} ({path})")

    return errors, warnings


def main() -> int:
    errors, warnings = validate()
    print("=== Visual Pozdr preflight ===")
    for warning in warnings:
        print(f"WARNING: {warning}")
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        print("Startup stopped. Fix the errors and run start_visual_pozdr.bat again.")
        return 1
    print("OK: startup checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
