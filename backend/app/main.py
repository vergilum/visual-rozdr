from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import get_settings
from .storage import Storage
from .telegram_bot import TelegramBotRunner
from .video_worker import VideoWorker


settings = get_settings()
storage = Storage(settings)


class EventCreate(BaseModel):
    company_name: str = Field(min_length=1, max_length=120)
    event_name: str = Field(default="", max_length=120)
    screen_text: str = Field(default="С Новым годом!", max_length=240)


class EventUpdate(BaseModel):
    company_name: str | None = Field(default=None, min_length=1, max_length=120)
    event_name: str | None = Field(default=None, max_length=120)
    screen_text: str | None = Field(default=None, max_length=240)
    moderation_mode: str | None = None
    playback_mode: str | None = None
    reception_open: bool | None = None
    volume: int | None = Field(default=None, ge=0, le=100)
    transition_seconds: int | None = Field(default=None, ge=0, le=10)
    theme: str | None = Field(default=None, max_length=80)
    primary_color: str | None = Field(default=None, max_length=7)
    accent_color: str | None = Field(default=None, max_length=7)
    max_duration_sec: int | None = Field(default=None, ge=1, le=600)
    max_size_mb: int | None = Field(default=None, ge=1, le=2048)
    guest_limit: int | None = Field(default=None, ge=1, le=100)


class SubmissionUpdate(BaseModel):
    guest_display_name: str = Field(max_length=120)


class SubmissionTableUpdate(BaseModel):
    table_number: int = Field(ge=1, le=15)


class EventTablesUpdate(BaseModel):
    table_count: int = Field(ge=1, le=15)
    tasks: list[str] = Field(max_length=15)

class WallVisibilityUpdate(BaseModel):
    visible: bool


class QueueReorder(BaseModel):
    queue_item_ids: list[str]


class QueuePlayNow(BaseModel):
    screen_id: str | None = Field(default=None, max_length=120)


class PlaybackEvent(BaseModel):
    playback_id: str = Field(min_length=1)
    type: str
    reason: str | None = Field(default=None, max_length=500)
    screen_id: str | None = Field(default=None, max_length=120)


class PlaybackSkip(BaseModel):
    playback_id: str = Field(min_length=1)


class ScreenRegister(BaseModel):
    screen_id: str | None = Field(default=None, max_length=120)


class ScreenHeartbeat(BaseModel):
    state: str = Field(default="active", max_length=40)
    message: str | None = Field(default=None, max_length=500)
    progress_sec: float | None = Field(default=None, ge=0)
    duration_sec: float | None = Field(default=None, ge=0)


class ConnectionManager:
    def __init__(self) -> None:
        self.connections: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.connections.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self.connections.discard(websocket)

    async def broadcast_state(self) -> None:
        if not self.connections:
            return
        recipients = tuple(self.connections)
        state = await asyncio.to_thread(storage.get_state)

        async def deliver(websocket: WebSocket) -> None:
            try:
                await asyncio.wait_for(websocket.send_json({"type": "state", "payload": state}), timeout=1.0)
            except Exception:
                self.disconnect(websocket)
                try:
                    await asyncio.wait_for(websocket.close(code=1013), timeout=0.25)
                except Exception:
                    pass

        await asyncio.gather(*(deliver(websocket) for websocket in recipients))


manager = ConnectionManager()


@asynccontextmanager
async def lifespan(_: FastAPI):
    storage.acquire_app_lock()
    telegram_runner: TelegramBotRunner | None = None
    video_worker: VideoWorker | None = None
    try:
        storage.init()
        telegram_runner = TelegramBotRunner(settings, storage, manager.broadcast_state)
        video_worker = VideoWorker(settings, storage, manager.broadcast_state)
        await telegram_runner.start()
        await video_worker.start()
        yield
    finally:
        if video_worker is not None:
            await video_worker.stop()
        if telegram_runner is not None:
            await telegram_runner.stop()
        storage.release_app_lock()


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "stage": 9,
        "app": settings.app_name,
        "telegram": storage.telegram_bot_status,
        "telegram_error": storage.telegram_bot_error,
        "video_worker": storage.video_worker_status,
    }


@app.get("/api/state")
def get_state() -> dict[str, Any]:
    return storage.get_state()


@app.post("/api/events", status_code=201)
async def create_event(payload: EventCreate) -> dict[str, Any]:
    event = storage.create_event(payload.model_dump())
    await manager.broadcast_state()
    return event


@app.post("/api/events/{event_id}/activate")
async def activate_event(event_id: str) -> dict[str, Any]:
    try:
        event = storage.activate_event(event_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Event not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await manager.broadcast_state()
    return event


@app.post("/api/events/{event_id}/background")
async def upload_background(event_id: str, file: UploadFile = File(...)) -> dict[str, Any]:
    if file.content_type not in {"image/jpeg", "image/png", "image/webp"}:
        raise HTTPException(status_code=400, detail="Background must be JPEG, PNG or WebP")
    try:
        storage.get_event(event_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Event not found") from exc
    suffix = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}[file.content_type or "image/jpeg"]
    relative_path = f"events/{event_id}/background/background{suffix}"
    target = storage.data_dir / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    data = await file.read()
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Background is larger than 10 MB")
    target.write_bytes(data)
    event = storage.update_event(event_id, {"background_path": relative_path})
    await manager.broadcast_state()
    return event


@app.patch("/api/events/{event_id}")
async def update_event(event_id: str, payload: EventUpdate) -> dict[str, Any]:
    try:
        event = storage.update_event(event_id, payload.model_dump(exclude_unset=True))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Event not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await manager.broadcast_state()
    return event


@app.post("/api/submissions/{submission_id}/retry")
async def retry_submission(submission_id: str) -> dict[str, Any]:
    try:
        submission = storage.retry_submission(submission_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Submission not found") from exc
    await manager.broadcast_state()
    return submission


@app.patch("/api/submissions/{submission_id}")
async def update_submission(submission_id: str, payload: SubmissionUpdate) -> dict[str, Any]:
    try:
        submission = storage.update_submission_display_name(submission_id, payload.guest_display_name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Submission not found") from exc
    await manager.broadcast_state()
    return submission


@app.patch("/api/submissions/{submission_id}/table")
async def update_submission_table(submission_id: str, payload: SubmissionTableUpdate) -> dict[str, Any]:
    try:
        submission = storage.update_submission_table(submission_id, payload.table_number)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Submission not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await manager.broadcast_state()
    return submission


@app.put("/api/events/{event_id}/tables")
async def configure_event_tables(event_id: str, payload: EventTablesUpdate) -> list[dict[str, Any]]:
    try:
        tables = storage.configure_event_tables(event_id, payload.table_count, payload.tasks)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Event not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await manager.broadcast_state()
    return tables


@app.delete("/api/submissions/{submission_id}")
async def delete_submission(submission_id: str) -> dict[str, Any]:
    try:
        result = storage.delete_submission(submission_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Submission not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await manager.broadcast_state()
    return result


@app.post("/api/submissions/{submission_id}/approve")
async def approve_submission(submission_id: str) -> dict[str, Any]:
    try:
        submission = storage.approve_submission(submission_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Submission not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await manager.broadcast_state()
    return submission


@app.post("/api/submissions/{submission_id}/reject")
async def reject_submission(submission_id: str) -> dict[str, Any]:
    try:
        submission = storage.reject_submission(submission_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Submission not found") from exc
    await manager.broadcast_state()
    return submission


@app.post("/api/submissions/{submission_id}/replay")
async def replay_submission(submission_id: str) -> dict[str, Any]:
    try:
        item = storage.replay_submission(submission_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Submission not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await manager.broadcast_state()
    return item


@app.post("/api/submissions/{submission_id}/wall")
async def update_wall_visibility(submission_id: str, payload: WallVisibilityUpdate) -> dict[str, Any]:
    try:
        submission = storage.set_submission_wall_visibility(submission_id, payload.visible)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Submission not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await manager.broadcast_state()
    return submission


@app.post("/api/events/{event_id}/approve-all-pending")
async def approve_all_pending(event_id: str) -> dict[str, Any]:
    count = storage.approve_all_pending(event_id)
    await manager.broadcast_state()
    return {"approved": count}


@app.post("/api/events/{event_id}/queue/reorder")
async def reorder_queue(event_id: str, payload: QueueReorder) -> list[dict[str, Any]]:
    try:
        queue = storage.reorder_queue(event_id, payload.queue_item_ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await manager.broadcast_state()
    return queue


@app.post("/api/events/{event_id}/history/clear")
async def clear_history(event_id: str) -> dict[str, Any]:
    try:
        count = storage.clear_history(event_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Event not found") from exc
    await manager.broadcast_state()
    return {"cleared": count}


@app.post("/api/queue/{queue_item_id}/cancel")
async def cancel_queue_item(queue_item_id: str) -> dict[str, Any]:
    try:
        item = storage.cancel_queue_item(queue_item_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Queue item not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await manager.broadcast_state()
    return item


@app.post("/api/queue/{queue_item_id}/play-now")
async def play_queue_item_now(queue_item_id: str, payload: QueuePlayNow) -> dict[str, Any]:
    try:
        state = storage.play_queue_item_now(queue_item_id, payload.screen_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Queue item not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await manager.broadcast_state()
    return state


@app.post("/api/submissions/{submission_id}/play-now")
async def play_wall_submission_now(submission_id: str, payload: QueuePlayNow) -> dict[str, Any]:
    try:
        state = storage.play_wall_submission_now(submission_id, payload.screen_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Wall item not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await manager.broadcast_state()
    return state


@app.post("/api/submissions/{submission_id}/admin-play-now")
async def play_wall_submission_from_admin(submission_id: str) -> dict[str, Any]:
    try:
        state = storage.play_wall_submission_from_admin(submission_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Wall item not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await manager.broadcast_state()
    return state


@app.post("/api/events/{event_id}/playback/start")
async def start_playback(event_id: str) -> dict[str, Any]:
    try:
        state = storage.start_playback(event_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Event not found") from exc
    await manager.broadcast_state()
    return state


@app.post("/api/events/{event_id}/playback/pause")
async def pause_playback(event_id: str) -> dict[str, Any]:
    try:
        state = storage.pause_playback(event_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Event not found") from exc
    await manager.broadcast_state()
    return state


@app.post("/api/events/{event_id}/playback/resume")
async def resume_playback(event_id: str) -> dict[str, Any]:
    try:
        state = storage.resume_playback(event_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Event not found") from exc
    await manager.broadcast_state()
    return state


@app.post("/api/events/{event_id}/playback/events")
async def playback_event(event_id: str, payload: PlaybackEvent) -> dict[str, Any]:
    try:
        state = storage.handle_playback_event(event_id, payload.playback_id, payload.type, payload.reason, payload.screen_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Event not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await manager.broadcast_state()
    return state


@app.post("/api/screen/register")
async def register_screen(payload: ScreenRegister) -> dict[str, Any]:
    result = storage.register_screen(payload.screen_id)
    await manager.broadcast_state()
    return result


@app.post("/api/screen/{screen_id}/heartbeat")
async def heartbeat_screen(screen_id: str, payload: ScreenHeartbeat) -> dict[str, Any]:
    result = storage.heartbeat_screen(screen_id, payload.state, payload.message, payload.progress_sec, payload.duration_sec)
    await manager.broadcast_state()
    return result


@app.post("/api/screen/{screen_id}/release")
async def release_screen(screen_id: str) -> dict[str, Any]:
    result = storage.release_screen(screen_id)
    await manager.broadcast_state()
    return result


@app.post("/api/events/{event_id}/playback/skip")
async def skip_playback(event_id: str, payload: PlaybackSkip) -> dict[str, Any]:
    try:
        state = storage.skip_current(event_id, payload.playback_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Event not found") from exc
    await manager.broadcast_state()
    return state


@app.post("/api/events/{event_id}/playback/stop")
async def stop_playback(event_id: str) -> dict[str, Any]:
    try:
        state = storage.stop_show(event_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Event not found") from exc
    await manager.broadcast_state()
    return state


@app.get("/api/media/{relative_path:path}")
def get_media(relative_path: str) -> FileResponse:
    base = storage.data_dir.resolve()
    target = (base / relative_path).resolve()
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid media path") from exc
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="Media file not found")
    return FileResponse(target)


@app.websocket("/api/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await manager.connect(websocket)
    try:
        await websocket.send_json({"type": "state", "payload": storage.get_state()})
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(websocket)


frontend_dist = settings.resolved_frontend_dist_dir
if frontend_dist.exists():
    app.mount("/assets", StaticFiles(directory=frontend_dist / "assets"), name="assets")


@app.get("/{path:path}")
def serve_frontend(path: str) -> FileResponse:
    if frontend_dist.exists():
        target = frontend_dist / path
        if path and target.exists() and target.is_file():
            return FileResponse(target)
        return FileResponse(frontend_dist / "index.html")
    raise HTTPException(status_code=404, detail="Frontend is not built yet. Run npm run build in frontend/.")
