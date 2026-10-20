"""Check a frozen backend in an isolated profile, without a real Telegram token."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.request import urlopen

from websockets.asyncio.client import connect


ROOT = Path(__file__).resolve().parents[1]


async def check_websocket(base_url: str) -> None:
    async with connect(base_url.replace("http://", "ws://") + "/api/ws", open_timeout=5) as websocket:
        snapshot = json.loads(await asyncio.wait_for(websocket.recv(), timeout=5))
        if snapshot["type"] != "state" or "version" not in snapshot["payload"]:
            raise RuntimeError("WebSocket snapshot was not received")


def check(package_dir: Path) -> None:
    package_dir = package_dir.resolve()
    resources = package_dir / "Contents" / "Resources" if package_dir.suffix == ".app" else package_dir / "resources"
    suffix = ".exe" if sys.platform == "win32" else ""
    backend = resources / "backend" / f"visual-pozdr-backend{suffix}"
    frontend = resources / "frontend" / "dist"
    tools = resources / "runtime" / "ffmpeg"
    for required in [backend, frontend / "index.html", tools / f"ffmpeg{suffix}", tools / f"ffprobe{suffix}"]:
        if not required.is_file():
            raise RuntimeError(f"Missing packaged resource: {required}")

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"

    with tempfile.TemporaryDirectory(prefix="visual-pozdr-smoke-") as temporary:
        profile = Path(temporary)
        # Exercise the exact bundled encoders, audio, faststart and thumbnail
        # operations; a successful '-version' alone is insufficient.
        sample = profile / "sample.mp4"
        subprocess.run([
            str(tools / f"ffmpeg{suffix}"), "-v", "error", "-y",
            "-f", "lavfi", "-i", "color=c=blue:s=160x160:r=15",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
            "-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-preset", "veryfast", "-c:a", "aac", "-movflags", "+faststart", str(sample),
        ], check=True, timeout=30, capture_output=True)
        media = json.loads(subprocess.check_output([
            str(tools / f"ffprobe{suffix}"), "-v", "error", "-show_streams",
            "-of", "json", str(sample),
        ], timeout=15))
        if not {"h264", "aac"}.issubset({stream["codec_name"] for stream in media["streams"]}):
            raise RuntimeError("Bundled tools did not produce H.264/AAC")
        subprocess.run([
            str(tools / f"ffmpeg{suffix}"), "-v", "error", "-y", "-i", str(sample),
            "-frames:v", "1", "-q:v", "2", str(profile / "thumbnail.jpg"),
        ], check=True, timeout=30, capture_output=True)
        if not (profile / "thumbnail.jpg").stat().st_size:
            raise RuntimeError("Empty thumbnail")
        env = {
            **os.environ,
            "HOST": "127.0.0.1", "PORT": str(port),
            "DATA_DIR": str(profile / "data"),
            "DATABASE_PATH": str(profile / "data" / "app.sqlite3"),
            "FRONTEND_DIST_DIR": str(frontend),
            "FFMPEG_PATH": str(tools / f"ffmpeg{suffix}"),
            "FFPROBE_PATH": str(tools / f"ffprobe{suffix}"),
            "TELEGRAM_BOT_TOKEN": "", "TELEGRAM_BOT_USERNAME": "",
            "VISUAL_POZDR_DESKTOP_CONTROL": "1", "PYTHONUTF8": "1",
        }
        with (profile / "backend.log").open("w+b") as log:
            process = subprocess.Popen(
                [str(backend)], cwd=profile, env=env,
                stdin=subprocess.PIPE, stdout=log, stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )

            def get(route: str) -> bytes:
                with urlopen(base_url + route, timeout=2) as response:
                    return response.read()

            try:
                deadline = time.monotonic() + 30
                while True:
                    if process.poll() is not None:
                        raise RuntimeError(f"Packaged backend exited: {process.returncode}")
                    try:
                        health = json.loads(get("/api/health"))
                        if health["status"] == "ok":
                            break
                    except (URLError, TimeoutError):
                        pass
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Packaged backend health timeout")
                    time.sleep(0.2)

                for route in ["/admin", "/screen"]:
                    if b'<div id="root">' not in get(route):
                        raise RuntimeError(f"Frontend was not served at {route}")
                for asset in (frontend / "assets").iterdir():
                    if asset.suffix in {".js", ".css"} and not get("/assets/" + asset.name):
                        raise RuntimeError(f"Empty frontend asset: {asset.name}")
                state = json.loads(get("/api/state"))
                for tool in ["ffmpeg", "ffprobe"]:
                    if state["diagnostics"][tool] != "available":
                        raise RuntimeError(f"Packaged {tool} is unavailable")
                asyncio.run(check_websocket(base_url))
                process.stdin.write(b"shutdown\n")
                process.stdin.flush()
                process.wait(timeout=15)
                if process.returncode != 0:
                    raise RuntimeError(f"Backend shutdown returned {process.returncode}")
                print("PASS: packaged health, /admin, /screen, assets, WebSocket, H.264/AAC/JPEG, shutdown")
            except Exception:
                log.flush()
                log.seek(0)
                print(log.read().decode("utf-8", errors="replace"))
                raise
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                process.stdin.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package_dir", nargs="?", type=Path, default=ROOT / "artifacts" / "size-audit" / "win-unpacked")
    check(parser.parse_args().package_dir)
