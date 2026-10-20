import asyncio
import os
import sys
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from backend.app.config import Settings
from backend.app.main import ConnectionManager
from backend.app.storage import Storage
from backend.app.video_worker import VideoWorker, run_command


def make_store(tmp_path):
    config = Settings(_env_file=None, data_dir=tmp_path / 'data', telegram_bot_token=None)
    store = Storage(config)
    store.init()
    event = store.create_event({'company_name': 'Before'})
    submission, _ = store.create_telegram_submission({
        'event_id': event['id'], 'telegram_chat_id': 1, 'telegram_message_id': 1,
        'telegram_user_id': 1, 'telegram_file_id': 'test-file',
    })
    return store, event, submission


def test_broadcast_handles_connection_churn_and_slow_client(monkeypatch):
    from backend.app import main

    monkeypatch.setattr(main.storage, 'get_state', lambda: {'version': 1})

    async def scenario():
        manager = ConnectionManager()
        received = []

        class Healthy:
            async def send_json(self, payload):
                received.append(payload)

        healthy = Healthy()

        class Broken:
            async def send_json(self, payload):
                manager.connections.add(healthy)
                raise OSError('disconnected')

        class Slow:
            closed = False

            async def send_json(self, payload):
                await asyncio.Event().wait()

            async def close(self, code):
                self.closed = True

        broken, slow = Broken(), Slow()
        manager.connections.update([healthy, broken, slow])
        await asyncio.wait_for(manager.broadcast_state(), timeout=2)
        assert received == [{'type': 'state', 'payload': {'version': 1}}]
        assert manager.connections == {healthy}
        assert slow.closed

    asyncio.run(scenario())


@pytest.mark.parametrize('deleted', [False, True])
def test_worker_cleans_failed_or_deleted_job(tmp_path, monkeypatch, deleted):
    store, event, submission = make_store(tmp_path)

    class Downloader:
        async def download(self, file_id, target):
            target.write_bytes(b'incomplete video')

    worker = VideoWorker(store.settings, store, downloader=Downloader())

    async def validate(source, event):
        if deleted:
            store.delete_submission(submission['id'])
        raise ValueError('invalid video')

    monkeypatch.setattr(worker, '_probe_and_validate_source', validate)
    asyncio.run(worker.process_claimed_submission(store.claim_next_submission_for_processing()))
    assert not (store.data_dir / 'temp' / submission['id']).exists()
    if not deleted:
        assert store.get_submission(submission['id'])['preparation_status'] == 'failed'


def test_deleted_job_does_not_publish_after_transcoding(tmp_path, monkeypatch):
    from backend.app import video_worker

    store, event, submission = make_store(tmp_path)

    class Downloader:
        async def download(self, file_id, target):
            target.write_bytes(b'original')

    worker = VideoWorker(store.settings, store, downloader=Downloader())
    info = {'has_audio': False, 'has_video': True, 'duration_sec': 1}

    async def probe(*args):
        return info

    async def transcode(source, target, audio):
        target.write_bytes(b'playback')

    async def thumbnail(source, target, duration):
        target.write_bytes(b'thumbnail')
        store.delete_submission(submission['id'])

    monkeypatch.setattr(worker, '_probe_and_validate_source', probe)
    monkeypatch.setattr(video_worker, 'probe_media', probe)
    monkeypatch.setattr(worker, '_create_playback', transcode)
    monkeypatch.setattr(worker, '_create_thumbnail', thumbnail)
    asyncio.run(worker.process_claimed_submission(store.claim_next_submission_for_processing()))
    assert not list((store.data_dir / 'events').rglob('*.mp4'))
    assert not list((store.data_dir / 'events').rglob('*.jpg'))
    assert not (store.data_dir / 'temp' / submission['id']).exists()


def test_notification_failure_does_not_abort_worker(tmp_path):
    store, _, _ = make_store(tmp_path)

    async def broken():
        raise OSError('socket disconnected')

    asyncio.run(VideoWorker(store.settings, store, on_change=broken)._notify_change())


def test_cancelled_media_command_kills_and_reaps_child(monkeypatch):
    class Process:
        returncode = None
        killed = False
        reaped = False

        def __init__(self):
            self.started = asyncio.Event()
            self.finished = asyncio.Event()

        async def communicate(self):
            self.started.set()
            await self.finished.wait()
            self.reaped = True
            return b'', b''

        def kill(self):
            self.killed = True
            self.returncode = -1
            self.finished.set()

    async def scenario():
        child = Process()

        async def spawn(*args, **kwargs):
            return child

        monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
        task = asyncio.create_task(run_command(['fake-ffmpeg'], 30))
        await child.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert child.killed and child.reaped

    asyncio.run(scenario())


def test_snapshot_reads_one_database_version(tmp_path, monkeypatch):
    store, event, _ = make_store(tmp_path)
    with store.connect() as conn:
        conn.execute('PRAGMA journal_mode=WAL')
    original_connect = store.connect
    updated = False

    class Reader:
        def __init__(self, conn):
            self.conn = conn

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def execute(self, query, *args):
            nonlocal updated
            result = self.conn.execute(query, *args)
            if query.startswith('SELECT version FROM app_state') and not updated:
                updated = True
                with original_connect() as writer:
                    writer.execute("UPDATE events SET company_name = 'After' WHERE id = ?", (event['id'],))
                    writer.execute('UPDATE app_state SET version = version + 1')
                    writer.commit()
            return result

    @contextmanager
    def connect():
        with original_connect() as conn:
            yield Reader(conn)

    monkeypatch.setattr(store, 'connect', connect)
    snapshot = store.get_state()
    assert updated
    assert snapshot['active_event']['company_name'] == 'Before'
    assert store.get_state()['active_event']['company_name'] == 'After'


def test_desktop_control_pipe_gracefully_stops_backend(tmp_path):
    import socket

    root = Path(__file__).resolve().parents[2]
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]

    async def scenario():
        child = await asyncio.create_subprocess_exec(
            sys.executable, str(root / 'run_server.py'), cwd=tmp_path,
            env={**os.environ, 'HOST': '127.0.0.1', 'PORT': str(port),
                 'DATA_DIR': str(tmp_path / 'data'), 'TELEGRAM_BOT_TOKEN': '',
                 'FRONTEND_DIST_DIR': str(root / 'frontend' / 'dist'),
                 'VISUAL_POZDR_DESKTOP_CONTROL': '1'},
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        async def collect_output():
            stdout, stderr, _ = await asyncio.gather(child.stdout.read(), child.stderr.read(), child.wait())
            return stdout, stderr

        output = asyncio.create_task(collect_output())
        try:
            async with httpx.AsyncClient() as client:
                async def ready():
                    while child.returncode is None:
                        try:
                            response = await client.get(f'http://127.0.0.1:{port}/api/health')
                            if response.status_code == 200:
                                return
                        except httpx.HTTPError:
                            pass
                        await asyncio.sleep(0.1)
                    raise AssertionError('Backend exited before readiness')

                await asyncio.wait_for(ready(), timeout=20)
            child.stdin.write(b'shutdown\n')
            await child.stdin.drain()
            _, stderr = await asyncio.wait_for(asyncio.shield(output), timeout=10)
            assert child.returncode == 0, stderr.decode(errors='replace')
            assert b'Application shutdown complete' in stderr
        finally:
            if child.returncode is None:
                child.kill()
            await output

    asyncio.run(scenario())
