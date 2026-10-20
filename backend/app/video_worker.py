from __future__ import annotations

import asyncio
import json
import logging
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol

from aiogram import Bot

from .config import Settings
from .storage import Storage

logger = logging.getLogger(__name__)

class ProcessingError(Exception):
    pass


class Downloader(Protocol):
    async def download(self, file_id: str, target: Path) -> None:
        ...


class TelegramDownloader:
    def __init__(self, bot: Bot):
        self.bot = bot

    async def download(self, file_id: str, target: Path) -> None:
        telegram_file = await self.bot.get_file(file_id)
        if telegram_file.file_path is None:
            raise ProcessingError("Telegram не вернул путь к файлу")
        await self.bot.download_file(telegram_file.file_path, destination=target)


class VideoWorker:
    def __init__(
        self,
        settings: Settings,
        storage: Storage,
        on_change: Callable[[], Awaitable[None]] | None = None,
        downloader: Downloader | None = None,
    ):
        self.settings = settings
        self.storage = storage
        self.on_change = on_change
        self.downloader = downloader
        self.bot: Bot | None = None
        self.task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        self.storage.video_worker_status = self._initial_status()
        self.storage.video_worker_error = self._tool_error()
        if self.downloader is None and self.settings.telegram_bot_token:
            self.bot = Bot(token=self.settings.telegram_bot_token)
            self.downloader = TelegramDownloader(self.bot)
        self.task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        if self.bot is not None:
            await self.bot.session.close()

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                if not self._tools_available():
                    self.storage.video_worker_status = "tools_missing"
                    self.storage.video_worker_error = self._tool_error()
                    await asyncio.sleep(self.settings.video_worker_poll_interval_sec)
                    continue
                if self.downloader is None:
                    self.storage.video_worker_status = "not_configured"
                    self.storage.video_worker_error = "TELEGRAM_BOT_TOKEN не настроен"
                    await asyncio.sleep(self.settings.video_worker_poll_interval_sec)
                    continue
                submission = self.storage.claim_next_submission_for_processing()
                if submission is None:
                    self.storage.video_worker_status = "idle"
                    self.storage.video_worker_error = None
                    await asyncio.sleep(self.settings.video_worker_poll_interval_sec)
                    continue
                self.storage.video_worker_status = "processing"
                await self._notify_change()
                await self.process_claimed_submission(submission)
                await self._notify_change()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.storage.video_worker_status = "error"
                self.storage.video_worker_error = sanitize_error(str(exc), self.settings.telegram_bot_token)
                await asyncio.sleep(self.settings.video_worker_poll_interval_sec)

    async def process_claimed_submission(self, submission: dict[str, Any]) -> None:
        submission_id = submission["id"]
        temp_dir = self.storage.data_dir / "temp" / submission_id
        moved_paths: list[Path] = []
        published = False
        try:
            if temp_dir.exists():
                shutil.rmtree(temp_dir)
            temp_dir.mkdir(parents=True, exist_ok=True)

            source = temp_dir / "source.mp4"
            await self._download_with_retries(submission["telegram_file_id"], source)

            event = self.storage.get_event(submission["event_id"])
            source_info = await self._probe_and_validate_source(source, event)
            self.storage.mark_submission_processing(submission_id)
            await self._notify_change()

            playback_temp = temp_dir / "playback.mp4"
            thumb_temp = temp_dir / "thumbnail.jpg"
            await self._create_playback(source, playback_temp, source_info["has_audio"])
            playback_info = await probe_media(playback_temp, self.settings.ffprobe_timeout_sec, self.settings.ffprobe_binary)
            validate_video_info(playback_info)
            await self._create_thumbnail(playback_temp, thumb_temp, playback_info["duration_sec"])

            # Deletion during an awaited download/transcode must not publish orphans.
            self.storage.get_submission(submission_id)
            event_dir = self.storage.data_dir / "events" / submission["event_id"]
            original_rel = Path("events") / submission["event_id"] / "originals" / f"{submission_id}.mp4"
            playback_rel = Path("events") / submission["event_id"] / "playback" / f"{submission_id}.mp4"
            thumbnail_rel = Path("events") / submission["event_id"] / "thumbnails" / f"{submission_id}.jpg"
            for directory in ["originals", "playback", "thumbnails"]:
                (event_dir / directory).mkdir(parents=True, exist_ok=True)
            for temporary, relative in [(source, original_rel), (playback_temp, playback_rel), (thumb_temp, thumbnail_rel)]:
                destination = self.storage.data_dir / relative
                shutil.move(str(temporary), destination)
                moved_paths.append(destination)
            ready_submission = self.storage.mark_submission_ready(
                submission_id,
                original_path=original_rel.as_posix(),
                playback_path=playback_rel.as_posix(),
                thumbnail_path=thumbnail_rel.as_posix(),
                duration_sec=playback_info["duration_sec"],
            )
            published = True
            ready_event = self.storage.get_event(ready_submission["event_id"])
            if ready_event["moderation_mode"] == "auto":
                self.storage.approve_submission(submission_id)
        except Exception as exc:
            try:
                self.storage.mark_submission_failed(submission_id, sanitize_error(str(exc), self.settings.telegram_bot_token))
            except KeyError:
                # The operator may delete a job while it is being prepared.
                pass
        finally:
            if not published:
                for destination in moved_paths:
                    destination.unlink(missing_ok=True)
            shutil.rmtree(temp_dir, ignore_errors=True)

    async def _download_with_retries(self, file_id: str, target: Path) -> None:
        if self.downloader is None:
            raise ProcessingError("Telegram downloader не настроен")
        last_error: Exception | None = None
        for attempt in range(self.settings.video_processing_retries + 1):
            try:
                await asyncio.wait_for(self.downloader.download(file_id, target), timeout=self.settings.telegram_download_timeout_sec)
                return
            except Exception as exc:
                last_error = exc
                if attempt >= self.settings.video_processing_retries:
                    break
                await asyncio.sleep(self.settings.video_processing_retry_delay_sec)
        raise ProcessingError(f"Не удалось скачать файл из Telegram: {last_error}")

    async def _probe_and_validate_source(self, source: Path, event: dict[str, Any]) -> dict[str, Any]:
        actual_size = source.stat().st_size
        max_size = event["max_size_mb"] * 1024 * 1024
        if actual_size > max_size:
            raise ProcessingError(f"Файл больше лимита: {actual_size} байт > {max_size} байт")
        info = await probe_media(source, self.settings.ffprobe_timeout_sec, self.settings.ffprobe_binary)
        validate_video_info(info)
        if info["duration_sec"] > event["max_duration_sec"]:
            raise ProcessingError(f"Длительность больше лимита: {info['duration_sec']:.1f} сек > {event['max_duration_sec']} сек")
        return info

    async def _create_playback(self, source: Path, target: Path, has_audio: bool) -> None:
        command = [
            self.settings.ffmpeg_binary or "ffmpeg",
            "-y",
            "-i",
            str(source),
            "-map",
            "0:v:0",
        ]
        if has_audio:
            command.extend(["-map", "0:a:0", "-c:a", "aac", "-b:a", "128k"])
        else:
            command.append("-an")
        command.extend([
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-preset",
            "veryfast",
            "-movflags",
            "+faststart",
            str(target),
        ])
        await run_command(command, self.settings.ffmpeg_timeout_sec)

    async def _create_thumbnail(self, source: Path, target: Path, duration_sec: float) -> None:
        seek = max(0.0, min(duration_sec / 2, 1.0))
        await run_command(
            [self.settings.ffmpeg_binary or "ffmpeg", "-y", "-ss", str(seek), "-i", str(source), "-frames:v", "1", "-q:v", "2", str(target)],
            self.settings.ffmpeg_timeout_sec,
        )

    def _tools_available(self) -> bool:
        return self.settings.ffmpeg_binary is not None and self.settings.ffprobe_binary is not None

    def _tool_error(self) -> str | None:
        missing = [name for name, binary in [("ffmpeg", self.settings.ffmpeg_binary), ("ffprobe", self.settings.ffprobe_binary)] if binary is None]
        if not missing:
            return None
        return "Не найдены инструменты подготовки: " + ", ".join(missing) + ". Проверьте runtime/ffmpeg в установленном приложении или PATH в режиме разработки."

    def _initial_status(self) -> str:
        if not self._tools_available():
            return "tools_missing"
        if not self.settings.telegram_bot_token and self.downloader is None:
            return "not_configured"
        return "starting"

    async def _notify_change(self) -> None:
        if self.on_change is not None:
            try:
                await self.on_change()
            except Exception:
                # Notifications must not strand an already claimed video job.
                logger.warning("Video state notification failed")


async def probe_media(path: Path, timeout_sec: int, ffprobe_binary: str | None = None) -> dict[str, Any]:
    output = await run_command(
        [ffprobe_binary or "ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        timeout_sec,
    )
    try:
        data = json.loads(output)
    except json.JSONDecodeError as exc:
        raise ProcessingError("ffprobe вернул некорректный JSON") from exc
    streams = data.get("streams", [])
    video_streams = [stream for stream in streams if stream.get("codec_type") == "video"]
    audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]
    duration = data.get("format", {}).get("duration")
    if duration is None and video_streams:
        duration = video_streams[0].get("duration")
    try:
        duration_sec = float(duration or 0)
    except ValueError:
        duration_sec = 0
    return {
        "duration_sec": duration_sec,
        "has_video": bool(video_streams),
        "has_audio": bool(audio_streams),
        "video_codec": video_streams[0].get("codec_name") if video_streams else None,
    }


def validate_video_info(info: dict[str, Any]) -> None:
    if not info["has_video"]:
        raise ProcessingError("В файле нет видеодорожки")
    if info["duration_sec"] <= 0:
        raise ProcessingError("Не удалось определить длительность видео")


async def run_command(command: list[str], timeout_sec: int) -> str:
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise ProcessingError(f"Инструмент не найден: {command[0]}") from exc
    communication = asyncio.create_task(process.communicate())
    try:
        stdout, stderr = await asyncio.wait_for(asyncio.shield(communication), timeout=timeout_sec)
    except (TimeoutError, asyncio.CancelledError) as exc:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await communication
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise ProcessingError(f"Тайм-аут команды {command[0]}") from exc
    if process.returncode != 0:
        message = stderr.decode("utf-8", errors="replace").strip().splitlines()[-1:]
        raise ProcessingError(f"{command[0]} завершился с ошибкой: {' '.join(message) or process.returncode}")
    return stdout.decode("utf-8", errors="replace")


def sanitize_error(message: str, token: str | None) -> str:
    if token:
        message = message.replace(token, "<telegram-token>")
    return message[:500]
