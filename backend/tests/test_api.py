import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.main import app, settings, storage
from scripts.preflight_check import parse_env, validate
from backend.app.telegram_bot import (
    CONNECTED_TEXT,
    LOW_DISK_TEXT,
    NEED_BINDING_TEXT,
    RECEIVED_TEXT,
    RECEPTION_CLOSED_TEXT,
    START_WITHOUT_CODE,
    TelegramReceptionService,
    TelegramVideoNotePayload,
)
from backend.app.video_worker import VideoWorker


def test_health_and_event_lifecycle(tmp_path):
    settings.telegram_bot_token = None
    storage.data_dir = tmp_path / "data"
    storage.db_path = storage.data_dir / "app.sqlite3"
    storage.init()

    client = TestClient(app)
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["stage"] == 9
    assert health.json()["telegram"] == "not_configured"

    created = client.post("/api/events", json={"company_name": "ACME"})
    assert created.status_code == 201
    event = created.json()
    assert event["company_name"] == "ACME"
    assert event["active"] is True
    assert event["theme"] == "new_year"

    updated = client.patch(f"/api/events/{event['id']}", json={"screen_text": "Hello", "reception_open": True})
    assert updated.status_code == 200
    assert updated.json()["screen_text"] == "Hello"
    assert updated.json()["reception_open"] is True

    state = client.get("/api/state")
    assert state.status_code == 200
    assert state.json()["active_event"]["id"] == event["id"]
    assert state.json()["version"] >= 2
    assert state.json()["diagnostics"]["bot"] == "not_configured"


def test_event_theme_values_are_normalized_and_validated(tmp_path):
    store = reset_storage(tmp_path)
    event = store.create_event({"company_name": "ACME"})

    neutral = store.update_event(event["id"], {"theme": "neutral"})
    assert neutral["theme"] == "neutral"

    new_year = store.update_event(event["id"], {"theme": "simple_new_year"})
    assert new_year["theme"] == "new_year"

    with pytest.raises(ValueError):
        store.update_event(event["id"], {"theme": "unknown"})


def reset_storage(tmp_path):
    storage.data_dir = tmp_path / "data"
    storage.db_path = storage.data_dir / "app.sqlite3"
    storage.init()
    return storage


def test_start_without_code_does_not_bind_guest(tmp_path):
    store = reset_storage(tmp_path)
    service = TelegramReceptionService(store)

    response = service.connect_guest(telegram_user_id=1001, code=None)

    assert response == START_WITHOUT_CODE
    assert store.get_guest_binding(1001) is None


def test_start_with_code_binds_guest_when_reception_open(tmp_path):
    store = reset_storage(tmp_path)
    event = store.create_event({"company_name": "ACME"})
    store.update_event(event["id"], {"reception_open": True})
    service = TelegramReceptionService(store)

    response = service.connect_guest(telegram_user_id=1001, code=event["code"])

    assert response == CONNECTED_TEXT
    assert store.get_guest_binding(1001)["event_id"] == event["id"]


def test_closed_reception_does_not_bind_or_accept_video_note(tmp_path):
    store = reset_storage(tmp_path)
    event = store.create_event({"company_name": "ACME"})
    service = TelegramReceptionService(store)

    response = service.connect_guest(telegram_user_id=1001, code=event["code"])

    assert response == RECEPTION_CLOSED_TEXT
    assert store.get_guest_binding(1001) is None

    store.bind_guest(1001, event["id"])
    response, created = service.receive_video_note(make_video_payload())

    assert response == RECEPTION_CLOSED_TEXT
    assert created is False
    assert store.get_state()["counts"]["submissions"] == 0


def test_video_note_saved_as_received_and_duplicate_is_ignored(tmp_path):
    store = reset_storage(tmp_path)
    event = store.create_event({"company_name": "ACME"})
    store.update_event(event["id"], {"reception_open": True})
    store.bind_guest(1001, event["id"])
    store.configure_event_tables(event["id"], 2, ["Тост", "Песня"])
    service = TelegramReceptionService(store)
    payload = make_video_payload()

    response, created = service.receive_video_note(payload)

    assert response == RECEIVED_TEXT
    assert created is True
    assert store.get_state()["counts"]["submissions"] == 0
    pending = store.get_pending_table_selection_by_message(payload.telegram_chat_id, payload.telegram_message_id)
    assert pending is not None
    submission = store.confirm_pending_table_selection(pending["id"], payload.telegram_user_id, 2)
    assert submission["table_number"] == 2
    assert submission["table_task"] == "Песня"
    assert store.get_state()["counts"]["submissions"] == 1
    recent = store.get_state()["recent_submissions"]
    assert len(recent) == 1
    assert recent[0]["table_number"] == 2
    assert recent[0]["table_task"] == "Песня"
    assert recent[0]["telegram_message_id"] == payload.telegram_message_id
    assert recent[0]["preparation_status"] == "received"
    submission = store.get_submission_by_telegram_message(payload.telegram_chat_id, payload.telegram_message_id)
    assert submission is not None
    assert submission["event_id"] == event["id"]
    assert submission["telegram_user_id"] == 1001
    assert submission["telegram_file_id"] == "file-id"
    assert submission["telegram_file_unique_id"] == "unique-id"
    assert submission["telegram_file_size"] == 12345
    assert submission["telegram_duration_sec"] == 12
    assert submission["preparation_status"] == "received"

    response, created = service.receive_video_note(payload)

    assert response == RECEIVED_TEXT
    assert created is False
    assert store.get_state()["counts"]["submissions"] == 1


def test_pending_table_selection_rechecks_active_event_and_free_disk(tmp_path, monkeypatch):
    store = reset_storage(tmp_path)
    first_event = store.create_event({"company_name": "First"})
    store.update_event(first_event["id"], {"reception_open": True})
    store.configure_event_tables(first_event["id"], 1, [""])
    payload = make_video_payload()
    pending, created = store.create_pending_table_selection({"event_id": first_event["id"], **payload.__dict__})
    assert created is True

    second_event = store.create_event({"company_name": "Second"})
    store.activate_event(second_event["id"])
    with pytest.raises(ValueError, match="Приём"):
        store.confirm_pending_table_selection(pending["id"], payload.telegram_user_id, 1)

    store.activate_event(first_event["id"])
    monkeypatch.setattr(store, "has_min_free_disk", lambda: False)
    with pytest.raises(ValueError, match="Недостаточно"):
        store.confirm_pending_table_selection(pending["id"], payload.telegram_user_id, 1)


def test_failed_submission_does_not_consume_guest_limit_at_table_confirmation(tmp_path):
    store = reset_storage(tmp_path)
    event = store.create_event({"company_name": "ACME"})
    store.update_event(event["id"], {"reception_open": True, "guest_limit": 1})
    store.configure_event_tables(event["id"], 1, [""])
    first_payload = make_video_payload()
    first_pending, _ = store.create_pending_table_selection({"event_id": event["id"], **first_payload.__dict__})
    first_submission = store.confirm_pending_table_selection(first_pending["id"], first_payload.telegram_user_id, 1)
    store.mark_submission_failed(first_submission["id"], "broken")

    second_payload = TelegramVideoNotePayload(
        telegram_user_id=first_payload.telegram_user_id,
        telegram_chat_id=first_payload.telegram_chat_id,
        telegram_message_id=first_payload.telegram_message_id + 1,
        guest_display_name=first_payload.guest_display_name,
        telegram_file_id=first_payload.telegram_file_id,
        telegram_file_unique_id=first_payload.telegram_file_unique_id,
        telegram_file_size=first_payload.telegram_file_size,
        telegram_duration_sec=first_payload.telegram_duration_sec,
    )
    second_pending, _ = store.create_pending_table_selection({"event_id": event["id"], **second_payload.__dict__})
    second_submission = store.confirm_pending_table_selection(second_pending["id"], second_payload.telegram_user_id, 1)

    assert second_submission["preparation_status"] == "received"

def test_unbound_guest_video_note_is_rejected(tmp_path):
    store = reset_storage(tmp_path)
    event = store.create_event({"company_name": "ACME"})
    store.update_event(event["id"], {"reception_open": True})
    service = TelegramReceptionService(store)

    response, created = service.receive_video_note(make_video_payload())

    assert response == NEED_BINDING_TEXT
    assert created is False
    assert store.get_state()["counts"]["submissions"] == 0


def test_low_disk_does_not_accept_video_note(tmp_path, monkeypatch):
    store = reset_storage(tmp_path)
    event = store.create_event({"company_name": "ACME"})
    store.update_event(event["id"], {"reception_open": True})
    store.bind_guest(1001, event["id"])
    store.configure_event_tables(event["id"], 1, [""])
    service = TelegramReceptionService(store)
    monkeypatch.setattr(store, "has_min_free_disk", lambda: False)

    response, created = service.receive_video_note(make_video_payload())

    assert response == LOW_DISK_TEXT
    assert created is False
    assert store.get_state()["counts"]["submissions"] == 0


def test_table_is_selected_for_each_received_video(tmp_path):
    store = reset_storage(tmp_path)
    event = store.create_event({"company_name": "ACME"})
    store.update_event(event["id"], {"reception_open": True})
    store.configure_event_tables(event["id"], 2, ["Задание 1", "Задание 2"])
    store.bind_guest(1001, event["id"])
    service = TelegramReceptionService(store)

    first_payload = make_video_payload()
    response, created = service.receive_video_note(first_payload)
    assert response == RECEIVED_TEXT
    assert created is True
    first = store.get_pending_table_selection_by_message(first_payload.telegram_chat_id, first_payload.telegram_message_id)
    assert first is not None
    selected_first = store.confirm_pending_table_selection(first["id"], first_payload.telegram_user_id, 1)
    assert selected_first["table_task"] == "Задание 1"

    second_payload = TelegramVideoNotePayload(**{**first_payload.__dict__, "telegram_message_id": first_payload.telegram_message_id + 1})
    response, created = service.receive_video_note(second_payload)
    assert response == RECEIVED_TEXT
    assert created is True
    second = store.get_pending_table_selection_by_message(second_payload.telegram_chat_id, second_payload.telegram_message_id)
    assert second is not None
    selected_second = store.confirm_pending_table_selection(second["id"], second_payload.telegram_user_id, 2)
    assert selected_second["table_task"] == "Задание 2"


def test_operator_can_correct_only_one_submission_table(tmp_path):
    store = reset_storage(tmp_path)
    event = store.create_event({"company_name": "ACME"})
    store.configure_event_tables(event["id"], 2, ["Первое", "Второе"])
    store.bind_guest(1001, event["id"])
    submission, _ = store.create_telegram_submission({**make_video_payload().__dict__, "event_id": event["id"], "table_number": 1, "table_task": "Первое"})

    updated = store.update_submission_table(submission["id"], 2)

    assert updated["table_number"] == 2
    assert updated["table_task"] == "Второе"
    assert store.get_guest_binding(1001)["event_id"] == event["id"]


def test_init_cleans_temp_directory(tmp_path):
    store = reset_storage(tmp_path)
    temp_job = store.data_dir / "temp" / "old-job"
    temp_job.mkdir(parents=True)
    (temp_job / "source.mp4").write_bytes(b"partial")
    (store.data_dir / "temp" / "loose.tmp").write_bytes(b"partial")

    store.init()

    assert list((store.data_dir / "temp").iterdir()) == []


def test_app_lock_prevents_second_instance(tmp_path):
    store = reset_storage(tmp_path)
    second_store = type(store)(settings)
    second_store.data_dir = store.data_dir
    second_store.db_path = store.db_path

    store.acquire_app_lock()
    try:
        with pytest.raises(RuntimeError):
            second_store.acquire_app_lock()
    finally:
        store.release_app_lock()


def make_video_payload() -> TelegramVideoNotePayload:
    return TelegramVideoNotePayload(
        telegram_user_id=1001,
        telegram_chat_id=2002,
        telegram_message_id=3003,
        guest_display_name="Test Guest",
        telegram_file_id="file-id",
        telegram_file_unique_id="unique-id",
        telegram_file_size=12345,
        telegram_duration_sec=12,
    )


class LocalFileDownloader:
    def __init__(self, source: Path):
        self.source = source

    async def download(self, file_id: str, target: Path) -> None:
        shutil.copyfile(self.source, target)


def create_received_submission(store, event, *, message_id=3003):
    submission, created = store.create_telegram_submission(
        {
            "event_id": event["id"],
            "telegram_chat_id": 2002,
            "telegram_message_id": message_id,
            "telegram_user_id": 1001,
            "guest_display_name": "Test Guest",
            "telegram_file_id": "file-id",
            "telegram_file_unique_id": "unique-id",
            "telegram_file_size": 12345,
            "telegram_duration_sec": 1,
        }
    )
    assert created is True
    return submission


def create_ready_submission(store, event, *, message_id=3003, name="Test Guest"):
    submission = create_received_submission(store, event, message_id=message_id)
    store.update_submission_display_name(submission["id"], name)
    return store.mark_submission_ready(
        submission["id"],
        original_path=f"events/{event['id']}/originals/{submission['id']}.mp4",
        playback_path=f"events/{event['id']}/playback/{submission['id']}.mp4",
        thumbnail_path=f"events/{event['id']}/thumbnails/{submission['id']}.jpg",
        duration_sec=1.0,
    )


def approve_and_add_to_wall(store, submission):
    store.approve_submission(submission["id"])
    return store.add_submission_to_wall(submission["id"])


def require_ffmpeg():
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg/ffprobe are not installed")


def make_test_video(path: Path, *, duration=1) -> None:
    require_ffmpeg()
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size=160x160:rate=15:duration={duration}",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def test_successful_video_processing_creates_ready_files(tmp_path):
    store = reset_storage(tmp_path)
    event = store.create_event({"company_name": "ACME"})
    create_received_submission(store, event)
    source = tmp_path / "source.mp4"
    make_test_video(source)
    claimed = store.claim_next_submission_for_processing()
    assert claimed is not None
    worker = VideoWorker(settings, store, downloader=LocalFileDownloader(source))

    asyncio.run(worker.process_claimed_submission(claimed))

    ready = store.get_submission(claimed["id"])
    assert ready["preparation_status"] == "ready"
    assert ready["original_path"]
    assert ready["playback_path"]
    assert ready["thumbnail_path"]
    assert (store.data_dir / ready["original_path"]).exists()
    assert (store.data_dir / ready["playback_path"]).exists()
    assert (store.data_dir / ready["thumbnail_path"]).exists()


def test_corrupted_video_becomes_failed(tmp_path):
    require_ffmpeg()
    store = reset_storage(tmp_path)
    event = store.create_event({"company_name": "ACME"})
    create_received_submission(store, event)
    source = tmp_path / "broken.mp4"
    source.write_bytes(b"not a video")
    claimed = store.claim_next_submission_for_processing()
    assert claimed is not None
    worker = VideoWorker(settings, store, downloader=LocalFileDownloader(source))

    asyncio.run(worker.process_claimed_submission(claimed))

    failed = store.get_submission(claimed["id"])
    assert failed["preparation_status"] == "failed"
    assert failed["error_reason"]


def test_duration_limit_becomes_failed(tmp_path):
    store = reset_storage(tmp_path)
    event = store.create_event({"company_name": "ACME"})
    store.update_event(event["id"], {"max_duration_sec": 1})
    create_received_submission(store, event)
    source = tmp_path / "long.mp4"
    make_test_video(source, duration=2)
    claimed = store.claim_next_submission_for_processing()
    assert claimed is not None
    worker = VideoWorker(settings, store, downloader=LocalFileDownloader(source))

    asyncio.run(worker.process_claimed_submission(claimed))

    failed = store.get_submission(claimed["id"])
    assert failed["preparation_status"] == "failed"
    assert "Длительность" in failed["error_reason"]


def test_incomplete_processing_is_recovered_on_init(tmp_path):
    store = reset_storage(tmp_path)
    event = store.create_event({"company_name": "ACME"})
    create_received_submission(store, event)
    claimed = store.claim_next_submission_for_processing()
    assert claimed is not None
    assert store.get_submission(claimed["id"])["preparation_status"] == "downloading"

    store.init()

    assert store.get_submission(claimed["id"])["preparation_status"] == "received"


def test_missing_tools_do_not_fail_waiting_submission(tmp_path, monkeypatch):
    store = reset_storage(tmp_path)
    event = store.create_event({"company_name": "ACME"})
    submission = create_received_submission(store, event)
    monkeypatch.setattr("backend.app.config.resolve_tool_binary", lambda *_: None)
    worker = VideoWorker(settings, store, downloader=LocalFileDownloader(tmp_path / "missing.mp4"))

    assert worker._tools_available() is False
    assert worker._initial_status() == "tools_missing"
    assert worker._tool_error() and "Не найдены инструменты подготовки" in worker._tool_error()
    assert store.get_submission(submission["id"])["preparation_status"] == "received"


def test_manual_moderation_approve_is_idempotent_and_does_not_queue(tmp_path):
    store = reset_storage(tmp_path)
    event = store.create_event({"company_name": "ACME"})
    submission = create_ready_submission(store, event)
    assert submission["moderation_status"] == "pending"

    store.approve_submission(submission["id"])
    store.approve_submission(submission["id"])
    state = store.get_state()
    assert state["queue"] == []
    assert state["wall"] == []
    assert store.get_submission(submission["id"])["moderation_status"] == "approved"

    store.reject_submission(submission["id"])
    state = store.get_state()
    assert state["queue"] == []
    assert store.get_submission(submission["id"])["moderation_status"] == "rejected"


def test_auto_moderation_after_ready_approves_without_queueing(tmp_path):
    store = reset_storage(tmp_path)
    event = store.create_event({"company_name": "ACME"})
    store.update_event(event["id"], {"moderation_mode": "auto"})
    submission = create_received_submission(store, event)
    ready = store.mark_submission_ready(
        submission["id"],
        original_path="events/e/originals/a.mp4",
        playback_path="events/e/playback/a.mp4",
        thumbnail_path="events/e/thumbnails/a.jpg",
        duration_sec=1.0,
    )
    if store.get_event(ready["event_id"])["moderation_mode"] == "auto":
        store.approve_submission(ready["id"])

    assert store.get_submission(submission["id"])["moderation_status"] == "approved"
    assert store.get_state()["queue"] == []


def test_switching_to_auto_does_not_approve_existing_ready_pending(tmp_path):
    store = reset_storage(tmp_path)
    event = store.create_event({"company_name": "ACME"})
    submission = create_ready_submission(store, event)

    store.update_event(event["id"], {"moderation_mode": "auto"})

    assert store.get_submission(submission["id"])["moderation_status"] == "pending"
    assert store.get_state()["queue"] == []


def test_approve_all_reorder_cancel_replay_and_playback_transitions(tmp_path):
    store = reset_storage(tmp_path)
    event = store.create_event({"company_name": "ACME"})
    first = create_ready_submission(store, event, message_id=1)
    second = create_ready_submission(store, event, message_id=2)

    assert store.approve_all_pending(event["id"]) == 2
    assert store.get_state()["queue"] == []

    store.replay_submission(first["id"])
    store.replay_submission(second["id"])
    queue = store.get_state()["queue"]
    assert [item["submission_id"] for item in queue] == [first["id"], second["id"]]

    store.reorder_queue(event["id"], [queue[1]["id"], queue[0]["id"]])
    reordered = store.get_state()["queue"]
    assert [item["submission_id"] for item in reordered] == [second["id"], first["id"]]

    store.cancel_queue_item(reordered[0]["id"])
    queue = store.get_state()["queue"]
    assert [item["submission_id"] for item in queue] == [first["id"]]

    playback = store.start_playback(event["id"])
    assert playback["state"] == "running"
    assert playback["current"]["submission_id"] == first["id"]
    completed = store.complete_current(event["id"], playback["current"]["playback_id"])
    assert completed == {"state": "idle", "current": None}


def test_running_show_cycles_completed_submissions_until_stopped(tmp_path):
    store = reset_storage(tmp_path)
    event = store.create_event({"company_name": "ACME"})
    first = create_ready_submission(store, event, message_id=1)
    second = create_ready_submission(store, event, message_id=2)
    approve_and_add_to_wall(store, first)
    approve_and_add_to_wall(store, second)

    playback = store.start_playback(event["id"])
    assert playback["current"]["submission_id"] == first["id"]

    playback = store.complete_current(event["id"], playback["current"]["playback_id"])
    assert playback["current"]["submission_id"] == second["id"]

    playback = store.complete_current(event["id"], playback["current"]["playback_id"])
    assert playback["state"] == "running"
    assert playback["current"]["submission_id"] == first["id"]

    stopped = store.stop_show(event["id"])
    assert stopped["state"] == "stopped"
    assert stopped["current"] is None


def test_newly_added_wall_submission_enters_running_auto_loop(tmp_path):
    store = reset_storage(tmp_path)
    event = store.create_event({"company_name": "ACME"})
    first = create_ready_submission(store, event, message_id=1)
    second = create_ready_submission(store, event, message_id=2)
    third = create_ready_submission(store, event, message_id=3)
    approve_and_add_to_wall(store, first)
    approve_and_add_to_wall(store, second)

    playback = store.start_playback(event["id"])
    playback = store.complete_current(event["id"], playback["current"]["playback_id"])
    assert playback["current"]["submission_id"] == second["id"]

    approve_and_add_to_wall(store, third)
    playback = store.complete_current(event["id"], playback["current"]["playback_id"])
    assert playback["current"]["submission_id"] == first["id"]

    playback = store.complete_current(event["id"], playback["current"]["playback_id"])
    assert playback["current"]["submission_id"] == third["id"]


def test_start_playback_cycles_when_queue_is_empty_but_history_exists(tmp_path):
    store = reset_storage(tmp_path)
    event = store.create_event({"company_name": "ACME"})
    first = create_ready_submission(store, event, message_id=1)
    approve_and_add_to_wall(store, first)

    playback = store.start_playback(event["id"])
    stopped = store.stop_show(event["id"])
    playback = store.start_playback(event["id"])
    completed = store.complete_current(event["id"], playback["current"]["playback_id"])
    stopped = store.stop_show(event["id"])
    store.cancel_queue_item(stopped["current"]["id"] if stopped["current"] else store.get_state()["queue"][0]["id"])

    assert completed["state"] == "running"
    assert store.get_state()["queue"] == []
    assert store.start_playback(event["id"])["current"]["submission_id"] == first["id"]


@pytest.mark.parametrize("video_count", [1, 3, 7])
def test_running_show_cycles_any_number_of_completed_submissions(tmp_path, video_count):
    store = reset_storage(tmp_path)
    event = store.create_event({"company_name": "ACME"})
    submissions = [create_ready_submission(store, event, message_id=index + 1) for index in range(video_count)]
    for submission in submissions:
        approve_and_add_to_wall(store, submission)

    playback = store.start_playback(event["id"])
    seen = []
    for _ in range(video_count * 2 + 1):
        seen.append(playback["current"]["submission_id"])
        playback = store.complete_current(event["id"], playback["current"]["playback_id"])

    expected = [submission["id"] for submission in submissions]
    assert seen[:video_count] == expected
    assert seen[video_count:video_count * 2] == expected
    assert playback["state"] == "running"


def test_completed_event_continues_cycle_even_if_event_state_is_idle(tmp_path):
    store = reset_storage(tmp_path)
    event = store.create_event({"company_name": "ACME"})
    first = create_ready_submission(store, event, message_id=1)
    second = create_ready_submission(store, event, message_id=2)
    approve_and_add_to_wall(store, first)
    approve_and_add_to_wall(store, second)

    playback = store.start_playback(event["id"])
    with store.connect() as conn:
        conn.execute("UPDATE events SET playback_state = 'idle' WHERE id = ?", (event["id"],))
        conn.commit()

    playback = store.complete_current(event["id"], playback["current"]["playback_id"])

    assert playback["state"] == "running"
    assert playback["current"]["submission_id"] == second["id"]


def test_skip_and_playback_error_continue_cycle_when_not_stopped(tmp_path):
    store = reset_storage(tmp_path)
    event = store.create_event({"company_name": "ACME"})
    first = create_ready_submission(store, event, message_id=1)
    second = create_ready_submission(store, event, message_id=2)
    approve_and_add_to_wall(store, first)
    approve_and_add_to_wall(store, second)

    playback = store.start_playback(event["id"])
    playback = store.complete_current(event["id"], playback["current"]["playback_id"])
    store.stop_show(event["id"])
    playback = store.start_playback(event["id"])
    skipped = store.skip_current(event["id"])
    assert skipped["state"] == "running"
    assert skipped["current"] is not None

    failed = store.fail_current(event["id"], skipped["current"]["playback_id"], "broken")
    assert failed["state"] == "running"
    assert failed["current"]["submission_id"] == first["id"]


def test_repeated_skip_for_an_old_playback_id_does_not_skip_the_next_video(tmp_path):
    store = reset_storage(tmp_path)
    event = store.create_event({"company_name": "ACME"})
    first = create_ready_submission(store, event, message_id=1)
    second = create_ready_submission(store, event, message_id=2)
    approve_and_add_to_wall(store, first)
    approve_and_add_to_wall(store, second)

    playback = store.start_playback(event["id"])
    first_playback_id = playback["current"]["playback_id"]
    skipped = store.skip_current(event["id"], first_playback_id)
    repeated = store.skip_current(event["id"], first_playback_id)

    assert skipped["current"]["submission_id"] == second["id"]
    assert repeated["current"]["submission_id"] == second["id"]
    assert [item["status"] for item in store.get_state()["history"]] == ["skipped"]


def test_removed_or_rejected_wall_item_does_not_reenter_auto_loop(tmp_path):
    store = reset_storage(tmp_path)
    event = store.create_event({"company_name": "ACME"})
    first = create_ready_submission(store, event, message_id=1)
    second = create_ready_submission(store, event, message_id=2)
    approve_and_add_to_wall(store, first)
    approve_and_add_to_wall(store, second)

    playback = store.start_playback(event["id"])
    store.remove_submission_from_wall(second["id"])
    playback = store.complete_current(event["id"], playback["current"]["playback_id"])

    assert playback["current"]["submission_id"] == first["id"]
    assert all(item["submission_id"] != second["id"] for item in store.get_state()["queue"])

    store.reject_submission(first["id"])
    playback = store.complete_current(event["id"], playback["current"]["playback_id"])
    assert playback == {"state": "idle", "current": None}


def test_playback_events_require_current_playback_id_and_are_idempotent(tmp_path):
    store = reset_storage(tmp_path)
    event = store.create_event({"company_name": "ACME"})
    first = create_ready_submission(store, event, message_id=1)
    second = create_ready_submission(store, event, message_id=2)
    approve_and_add_to_wall(store, first)
    approve_and_add_to_wall(store, second)

    playback = store.start_playback(event["id"])
    first_playback_id = playback["current"]["playback_id"]

    stale = store.handle_playback_event(event["id"], "stale-playback-id", "completed")

    assert stale["current"]["submission_id"] == first["id"]

    next_playback = store.handle_playback_event(event["id"], first_playback_id, "completed")

    assert next_playback["state"] == "running"
    assert next_playback["current"]["submission_id"] == second["id"]

    repeated = store.handle_playback_event(event["id"], first_playback_id, "completed")

    assert repeated["current"]["submission_id"] == second["id"]


def test_interactive_wall_returns_to_wall_after_explicit_completion(tmp_path):
    store = reset_storage(tmp_path)
    event = store.create_event({"company_name": "ACME"})
    store.update_event(event["id"], {"playback_mode": "interactive_wall"})
    first = create_ready_submission(store, event, message_id=1)
    second = create_ready_submission(store, event, message_id=2)
    approve_and_add_to_wall(store, first)
    approve_and_add_to_wall(store, second)
    store.register_screen("screen-1")

    playback = store.play_wall_submission_now(first["id"], screen_id="screen-1")

    assert playback["state"] == "running"
    assert playback["current"]["submission_id"] == first["id"]

    completed = store.complete_current(event["id"], playback["current"]["playback_id"])
    state = store.get_state()

    assert completed["state"] == "idle"
    assert completed["current"] is None
    assert state["queue"] == []
    assert [item["id"] for item in state["wall"]] == [first["id"], second["id"]]


def test_admin_starts_selected_wall_submission_without_interrupting_current_playback(tmp_path):
    store = reset_storage(tmp_path)
    event = store.create_event({"company_name": "ACME"})
    first = create_ready_submission(store, event, message_id=1)
    second = create_ready_submission(store, event, message_id=2)
    approve_and_add_to_wall(store, first)
    approve_and_add_to_wall(store, second)
    store.register_screen("screen-1")

    playback = store.play_wall_submission_from_admin(first["id"])

    assert playback["state"] == "running"
    assert playback["current"]["submission_id"] == first["id"]
    assert playback["current"]["playback_id"]
    assert playback["current"]["is_loop"] is False
    assert playback["current"]["hidden_from_history"] is False
    with pytest.raises(ValueError, match="Сначала остановите"):
        store.play_wall_submission_from_admin(second["id"])
    assert store.get_playback_state(event["id"])["current"]["submission_id"] == first["id"]


def test_admin_cannot_start_wall_submission_without_active_screen(tmp_path):
    store = reset_storage(tmp_path)
    event = store.create_event({"company_name": "ACME"})
    submission = create_ready_submission(store, event)
    approve_and_add_to_wall(store, submission)

    with pytest.raises(ValueError, match="Сначала откройте экран показа"):
        store.play_wall_submission_from_admin(submission["id"])


def test_interactive_wall_rejects_non_active_screen_and_auto_start(tmp_path):
    store = reset_storage(tmp_path)
    event = store.create_event({"company_name": "ACME"})
    store.update_event(event["id"], {"playback_mode": "interactive_wall"})
    submission = create_ready_submission(store, event)
    approve_and_add_to_wall(store, submission)
    store.register_screen("screen-1")
    store.register_screen("screen-2")

    playback = store.start_playback(event["id"])

    assert playback["state"] == "idle"
    assert playback["current"] is None
    with pytest.raises(ValueError):
        store.play_wall_submission_now(submission["id"], screen_id="screen-2")


def test_screen_registration_allows_only_one_active_screen(tmp_path):
    store = reset_storage(tmp_path)
    store.create_event({"company_name": "ACME"})

    first = store.register_screen("screen-1")
    second = store.register_screen("screen-2")

    assert first["status"] == "active"
    assert second["status"] == "secondary"
    assert store.get_state()["screen"]["screen_id"] == "screen-1"


def test_active_screen_rebinds_when_operator_switches_event(tmp_path):
    store = reset_storage(tmp_path)
    first_event = store.create_event({"company_name": "First"})
    second_event = store.create_event({"company_name": "Second"})
    submission = create_ready_submission(store, second_event)
    approve_and_add_to_wall(store, submission)
    store.register_screen("screen-1")

    store.activate_event(second_event["id"])
    playback = store.play_wall_submission_from_admin(submission["id"])
    store.release_screen("screen-1")

    assert playback["current"]["submission_id"] == submission["id"]
    state = store.get_state()
    assert state["active_event"]["id"] == second_event["id"]
    assert state["active_event"]["playback_state"] == "stopped"
    assert state["queue"][0]["submission_id"] == submission["id"]


def test_secondary_screen_becomes_active_after_the_first_screen_is_released(tmp_path):
    store = reset_storage(tmp_path)
    store.create_event({"company_name": "ACME"})

    store.register_screen("screen-1")
    store.register_screen("screen-2")
    store.release_screen("screen-1")

    result = store.heartbeat_screen("screen-2")

    assert result["status"] == "active"
    assert store.get_state()["screen"]["screen_id"] == "screen-2"


def test_lost_active_screen_stops_show_and_returns_playing_item(tmp_path):
    store = reset_storage(tmp_path)
    settings.screen_heartbeat_timeout_sec = 1
    event = store.create_event({"company_name": "ACME"})
    submission = create_ready_submission(store, event)
    approve_and_add_to_wall(store, submission)
    store.register_screen("screen-1")
    playback = store.start_playback(event["id"])
    assert playback["state"] == "running"

    old_seen = "2000-01-01T00:00:00+00:00"
    with store.connect() as conn:
        conn.execute("UPDATE playback_sessions SET last_seen_at = ? WHERE screen_id = ?", (old_seen, "screen-1"))
        conn.commit()

    state = store.get_state()

    assert state["active_event"]["playback_state"] == "stopped"
    assert state["queue"][0]["status"] == "queued"
    settings.screen_heartbeat_timeout_sec = 10


def test_secondary_screen_playback_event_does_not_advance_queue(tmp_path):
    store = reset_storage(tmp_path)
    event = store.create_event({"company_name": "ACME"})
    first = create_ready_submission(store, event, message_id=1)
    second = create_ready_submission(store, event, message_id=2)
    approve_and_add_to_wall(store, first)
    approve_and_add_to_wall(store, second)
    store.register_screen("screen-1")
    store.register_screen("screen-2")
    playback = store.start_playback(event["id"])
    playback_id = playback["current"]["playback_id"]

    state = store.handle_playback_event(event["id"], playback_id, "completed", screen_id="screen-2")

    assert state["current"]["submission_id"] == first["id"]


def test_state_includes_history_failed_submissions_and_screen_progress(tmp_path):
    store = reset_storage(tmp_path)
    event = store.create_event({"company_name": "ACME"})
    submission = create_ready_submission(store, event, message_id=1)
    approve_and_add_to_wall(store, submission)
    playback = store.start_playback(event["id"])
    store.complete_current(event["id"], playback["current"]["playback_id"])
    failed = create_received_submission(store, event, message_id=2)
    store.mark_submission_failed(failed["id"], "broken file")
    store.register_screen("screen-1")
    store.heartbeat_screen("screen-1", "playing", progress_sec=3.5, duration_sec=10.0)

    state = store.get_state()

    assert state["history"][0]["status"] == "completed"
    assert state["failed_submissions"][0]["id"] == failed["id"]
    assert state["screen"]["progress_sec"] == 3.5
    assert state["diagnostics"]["ffmpeg"] in {"available", "missing", "error"}
    assert state["diagnostics"]["ffprobe"] in {"available", "missing", "error"}
    assert "ffmpeg_version" in state["diagnostics"]
    assert "ffprobe_version" in state["diagnostics"]


def test_cannot_switch_event_while_playback_is_not_stopped(tmp_path):
    store = reset_storage(tmp_path)
    first_event = store.create_event({"company_name": "First"})
    second_event = store.create_event({"company_name": "Second"})
    submission = create_ready_submission(store, first_event)
    approve_and_add_to_wall(store, submission)
    store.start_playback(first_event["id"])

    with pytest.raises(ValueError):
        store.activate_event(second_event["id"])


def test_pause_resume_skip_and_stop_transitions(tmp_path):
    store = reset_storage(tmp_path)
    event = store.create_event({"company_name": "ACME"})
    first = create_ready_submission(store, event, message_id=1)
    second = create_ready_submission(store, event, message_id=2)
    assert store.approve_all_pending(event["id"]) == 2
    store.add_submission_to_wall(first["id"])
    store.add_submission_to_wall(second["id"])

    playback = store.start_playback(event["id"])
    assert playback["current"]["submission_id"] == first["id"]

    paused = store.pause_playback(event["id"])
    assert paused["state"] == "paused"
    assert paused["current"]["submission_id"] == first["id"]

    resumed = store.resume_playback(event["id"])
    assert resumed["state"] == "running"
    assert resumed["current"]["submission_id"] == first["id"]

    skipped = store.skip_current(event["id"])
    assert skipped["state"] == "running"
    assert skipped["current"]["submission_id"] == second["id"]

    stopped = store.stop_show(event["id"])
    assert stopped["state"] == "stopped"
    assert stopped["current"] is None
    assert store.get_state()["queue"][0]["submission_id"] == second["id"]


def test_idle_show_starts_newly_added_wall_submission(tmp_path):
    store = reset_storage(tmp_path)
    event = store.create_event({"company_name": "ACME"})

    empty = store.start_playback(event["id"])
    assert empty["state"] == "idle"

    submission = create_ready_submission(store, event)
    approve_and_add_to_wall(store, submission)

    playback = store.get_playback_state(event["id"])
    assert playback["state"] == "running"
    assert playback["current"]["submission_id"] == submission["id"]


def test_playing_item_recovers_to_queue_on_init(tmp_path):
    store = reset_storage(tmp_path)
    event = store.create_event({"company_name": "ACME"})
    submission = create_ready_submission(store, event)
    approve_and_add_to_wall(store, submission)
    store.start_playback(event["id"])

    store.init()

    state = store.get_state()
    assert state["active_event"]["playback_state"] == "stopped"
    assert state["queue"][0]["status"] == "queued"


def test_wall_is_operator_selected_and_ignores_duplicate_adds(tmp_path):
    store = reset_storage(tmp_path)
    event = store.create_event({"company_name": "ACME"})
    first = create_ready_submission(store, event, message_id=1, name="First")
    second = create_ready_submission(store, event, message_id=2, name="Second")
    store.approve_submission(first["id"])
    store.approve_submission(second["id"])

    store.add_submission_to_wall(first["id"])
    store.add_submission_to_wall(first["id"])
    wall = store.get_state()["wall"]
    assert [item["id"] for item in wall] == [first["id"]]

    store.add_submission_to_wall(second["id"])
    assert [item["id"] for item in store.get_state()["wall"]] == [first["id"], second["id"]]


def test_wall_add_requires_ready_approved_submission(tmp_path):
    store = reset_storage(tmp_path)
    event = store.create_event({"company_name": "ACME"})
    pending = create_ready_submission(store, event, message_id=1)
    failed = create_received_submission(store, event, message_id=2)
    store.mark_submission_failed(failed["id"], "broken file")

    with pytest.raises(ValueError):
        store.add_submission_to_wall(pending["id"])
    with pytest.raises(ValueError):
        store.add_submission_to_wall(failed["id"])

    store.approve_submission(pending["id"])
    store.add_submission_to_wall(pending["id"])
    assert [item["id"] for item in store.get_state()["wall"]] == [pending["id"]]


def test_clear_history_keeps_queue_and_wall(tmp_path):
    store = reset_storage(tmp_path)
    event = store.create_event({"company_name": "ACME"})
    submission = create_ready_submission(store, event)
    approve_and_add_to_wall(store, submission)
    playback = store.start_playback(event["id"])
    store.complete_current(event["id"], playback["current"]["playback_id"])

    state = store.get_state()
    assert state["history"][0]["status"] == "completed"
    assert state["queue"][0]["submission_id"] == submission["id"]
    assert state["wall"][0]["id"] == submission["id"]

    assert store.clear_history(event["id"]) == 1
    state = store.get_state()

    assert state["history"] == []
    assert state["queue"][0]["submission_id"] == submission["id"]
    assert state["wall"][0]["id"] == submission["id"]


def test_clear_history_api_broadcasts_state_and_keeps_queue_wall(tmp_path):
    store = reset_storage(tmp_path)
    event = store.create_event({"company_name": "ACME"})
    submission = create_ready_submission(store, event)
    approve_and_add_to_wall(store, submission)
    playback = store.start_playback(event["id"])
    store.complete_current(event["id"], playback["current"]["playback_id"])
    client = TestClient(app)

    response = client.post(f"/api/events/{event['id']}/history/clear")

    assert response.status_code == 200
    assert response.json() == {"cleared": 1}
    state = client.get("/api/state").json()
    assert state["history"] == []
    assert state["queue"][0]["submission_id"] == submission["id"]
    assert state["wall"][0]["id"] == submission["id"]

    assert store.clear_history(event["id"]) == 0
    state = store.get_state()

    assert state["history"] == []
    assert state["queue"][0]["submission_id"] == submission["id"]
    assert state["wall"][0]["id"] == submission["id"]


def test_wall_limit_is_fixed_at_twelve(tmp_path):
    store = reset_storage(tmp_path)
    event = store.create_event({"company_name": "ACME"})
    created_ids = []
    for message_id in range(13):
        submission = create_ready_submission(store, event, message_id=message_id + 1, name=f"Guest {message_id + 1}")
        created_ids.append(submission["id"])
        store.approve_submission(submission["id"])

    for submission_id in created_ids[:12]:
        store.add_submission_to_wall(submission_id)
    with pytest.raises(ValueError):
        store.add_submission_to_wall(created_ids[12])

    wall = store.get_state()["wall"]
    assert len(wall) == 12
    assert [item["id"] for item in wall] == created_ids[:12]


def test_wall_visibility_can_be_hidden_restored_and_survives_init(tmp_path):
    store = reset_storage(tmp_path)
    event = store.create_event({"company_name": "ACME"})
    submission = create_ready_submission(store, event)
    approve_and_add_to_wall(store, submission)

    assert len(store.get_state()["wall"]) == 1
    store.set_submission_wall_visibility(submission["id"], False)
    store.init()
    assert store.get_state()["wall"] == []
    hidden = store.get_submission(submission["id"])
    assert hidden["hidden_from_wall"] is True

    store.set_submission_wall_visibility(submission["id"], True)
    assert [item["id"] for item in store.get_state()["wall"]] == [submission["id"]]


def test_wall_visibility_api_broadcasts_state(tmp_path):
    store = reset_storage(tmp_path)
    event = store.create_event({"company_name": "ACME"})
    submission = create_ready_submission(store, event)
    approve_and_add_to_wall(store, submission)
    client = TestClient(app)

    response = client.post(f"/api/submissions/{submission['id']}/wall", json={"visible": False})

    assert response.status_code == 200
    assert response.json()["hidden_from_wall"] is True
    assert client.get("/api/state").json()["wall"] == []


def test_recent_submissions_are_scoped_to_active_event(tmp_path):
    store = reset_storage(tmp_path)
    first_event = store.create_event({"company_name": "First"})
    first = create_ready_submission(store, first_event, message_id=1)
    second_event = store.create_event({"company_name": "Second"})
    second = create_ready_submission(store, second_event, message_id=2)

    assert [item["id"] for item in store.get_state()["recent_submissions"]] == [first["id"]]

    store.activate_event(second_event["id"])

    assert [item["id"] for item in store.get_state()["recent_submissions"]] == [second["id"]]


def test_delete_submission_removes_database_rows_and_files(tmp_path):
    store = reset_storage(tmp_path)
    event = store.create_event({"company_name": "ACME"})
    submission = create_ready_submission(store, event)
    store.approve_submission(submission["id"])
    queue_item = store.replay_submission(submission["id"])

    for relative_path in [submission["original_path"], submission["playback_path"], submission["thumbnail_path"]]:
        path = store.data_dir / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"media")

    result = store.delete_submission(submission["id"])

    assert result == {"deleted": submission["id"]}
    with pytest.raises(KeyError):
        store.get_submission(submission["id"])
    with pytest.raises(KeyError):
        store.get_queue_item(queue_item["id"])
    state = store.get_state()
    assert state["recent_submissions"] == []
    assert state["queue"] == []
    assert state["history"] == []
    for relative_path in [submission["original_path"], submission["playback_path"], submission["thumbnail_path"]]:
        assert not (store.data_dir / relative_path).exists()


def test_delete_submission_removes_from_wall_automatically(tmp_path):
    store = reset_storage(tmp_path)
    event = store.create_event({"company_name": "ACME"})
    submission = create_ready_submission(store, event)
    approve_and_add_to_wall(store, submission)

    store.delete_submission(submission["id"])

    state = store.get_state()
    assert state["recent_submissions"] == []
    assert state["wall"] == []


def test_delete_submission_removes_cross_event_queue_rows(tmp_path):
    store = reset_storage(tmp_path)
    first_event = store.create_event({"company_name": "First"})
    second_event = store.create_event({"company_name": "Second"})
    submission = create_ready_submission(store, first_event)
    store.approve_submission(submission["id"])
    now = "2026-10-13T00:00:00+00:00"
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO queue_items (id, event_id, submission_id, position, status, created_at, updated_at) VALUES (?, ?, ?, 1, 'queued', ?, ?)",
            ("dirty-cross-event-queue", second_event["id"], submission["id"], now, now),
        )
        conn.commit()

    store.delete_submission(submission["id"])

    with store.connect() as conn:
        row = conn.execute("SELECT id FROM queue_items WHERE id = 'dirty-cross-event-queue'").fetchone()
    assert row is None


def test_delete_submission_rejects_currently_playing_item(tmp_path):
    store = reset_storage(tmp_path)
    event = store.create_event({"company_name": "ACME"})
    submission = create_ready_submission(store, event)
    store.approve_submission(submission["id"])
    store.replay_submission(submission["id"])
    store.start_playback(event["id"])

    with pytest.raises(ValueError, match="воспроизводится"):
        store.delete_submission(submission["id"])


def test_preflight_requires_env_file(tmp_path):
    errors, warnings = validate(tmp_path, check_port=False)

    assert warnings == []
    assert any(".env" in error for error in errors)


def test_preflight_accepts_minimal_local_env(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("HOST=127.0.0.1\nPORT=8123\nDATA_DIR=data\nMIN_FREE_DISK_MB=1\n", encoding="utf-8")
    (tmp_path / "frontend" / "dist").mkdir(parents=True)
    (tmp_path / "frontend" / "dist" / "index.html").write_text("ok", encoding="utf-8")
    monkeypatch.setattr("scripts.preflight_check.shutil.which", lambda tool: f"/bin/{tool}")

    errors, warnings = validate(tmp_path, check_port=False)

    assert errors == []
    assert any("TELEGRAM_BOT_TOKEN" in warning for warning in warnings)
    assert any("TELEGRAM_BOT_USERNAME" in warning for warning in warnings)


def test_preflight_parses_env_values(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("PORT=8010\nTELEGRAM_BOT_USERNAME='demo_bot'\n", encoding="utf-8")

    values = parse_env(env_path)

    assert values["PORT"] == "8010"
    assert values["TELEGRAM_BOT_USERNAME"] == "demo_bot"
