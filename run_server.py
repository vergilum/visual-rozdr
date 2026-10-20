import asyncio
import os
import sys
import threading

import uvicorn

from backend.app.main import app
from backend.app.config import get_settings


async def serve() -> None:
    settings = get_settings()
    server = uvicorn.Server(uvicorn.Config(
        app, host=settings.host, port=settings.port, reload=False,
        timeout_graceful_shutdown=3,
    ))
    if os.environ.get("VISUAL_POZDR_DESKTOP_CONTROL") == "1":
        loop = asyncio.get_running_loop()

        def watch_desktop() -> None:
            # Only the parent process owns this pipe; no public shutdown endpoint.
            for line in sys.stdin:
                if line.strip() == "shutdown":
                    break
            try:
                loop.call_soon_threadsafe(setattr, server, "should_exit", True)
            except RuntimeError:
                pass  # Event loop already stopped.

        threading.Thread(target=watch_desktop, daemon=True).start()
    await server.serve()


if __name__ == "__main__":
    asyncio.run(serve())
