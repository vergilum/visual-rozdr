import QRCode from 'qrcode';
import React, { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import './styles.css';

type Event = {
  id: string;
  code: string;
  company_name: string;
  event_name: string;
  screen_text: string;
  moderation_mode: 'manual' | 'auto';
  playback_mode: 'auto_loop' | 'interactive_wall';
  reception_open: boolean;
  volume: number;
  transition_seconds: number;
  theme: string;
  max_duration_sec: number;
  max_size_mb: number;
  guest_limit: number;
  table_count: number;
  primary_color: string;
  accent_color: string;
  background_path: string | null;
  playback_state: string;
  active: boolean;
  connection_link: string | null;
  created_at: string;
  updated_at: string;
};

type AppState = {
  version: number;
  active_event: Event | null;
  events: Event[];
  counts: {
    submissions: number;
    queued: number;
    playing: number;
  };
  recent_submissions: Submission[];
  pending_submissions: Submission[];
  queue: QueueItem[];
  history: QueueItem[];
  wall: Submission[];
  failed_submissions: Submission[];
  tables: EventTable[];
  playback: {
    state: string;
    current: QueueItem | null;
  };
  screen: {
    status: string;
    screen_id: string | null;
    last_seen_at: string | null;
    message: string | null;
    progress_sec: number | null;
    duration_sec: number | null;
    load_timeout_sec: number;
  };
  diagnostics: {
    bot: string;
    bot_error: string | null;
    video_worker: string;
    video_worker_error: string | null;
    screen: string;
    ffmpeg: string;
    ffmpeg_path: string | null;
    ffmpeg_version: string | null;
    ffprobe: string;
    ffprobe_path: string | null;
    ffprobe_version: string | null;
    database: string;
    free_disk_mb: number | null;
    min_free_disk_mb: number;
    disk: string;
  };
};

type QueueItem = {
  id: string;
  event_id: string;
  submission_id: string;
  position: number;
  status: 'queued' | 'playing' | 'completed' | 'skipped' | 'failed' | 'cancelled';
  playback_id: string | null;
  is_loop: boolean;
  hidden_from_history: boolean;
  error_reason: string | null;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
  updated_at: string;
  submission: Submission;
};

type Submission = {
  id: string;
  event_id: string;
  telegram_chat_id: number;
  telegram_message_id: number;
  telegram_user_id: number;
  guest_display_name: string;
  table_number: number | null;
  table_task: string;
  telegram_file_id: string | null;
  telegram_file_unique_id: string | null;
  telegram_file_size: number | null;
  telegram_duration_sec: number | null;
  duration_sec: number | null;
  preparation_status: 'received' | 'downloading' | 'processing' | 'ready' | 'failed';
  moderation_status: 'pending' | 'approved' | 'rejected';
  original_path: string | null;
  playback_path: string | null;
  thumbnail_path: string | null;
  error_reason: string | null;
  processing_attempts: number;
  processing_locked_at: string | null;
  hidden_from_wall: boolean;
  wall_completed_at?: string;
  created_at: string;
  updated_at: string;
};

type EventTable = { number: number; task: string };

type DesktopDisplay = {
  id: string;
  name: string;
  bounds: { x: number; y: number; width: number; height: number };
  workArea: { x: number; y: number; width: number; height: number };
  scaleFactor: number;
  primary: boolean;
};

type DesktopScreenState = {
  displays: DesktopDisplay[];
  selectedDisplayId: string;
  screenOpen: boolean;
  warning: string | null;
};

declare global {
  interface Window {
    desktop?: {
      getScreenState: () => Promise<DesktopScreenState>;
      selectScreenDisplay: (displayId: string) => Promise<DesktopScreenState>;
      openScreen: () => Promise<DesktopScreenState>;
      moveScreen: () => Promise<DesktopScreenState>;
      closeScreen: () => Promise<DesktopScreenState>;
      openConfiguration: () => Promise<void>;
      onScreenStateChanged: (listener: (state: DesktopScreenState) => void) => () => void;
    };
  }
}

const emptyState: AppState = {
  version: 0,
  active_event: null,
  events: [],
  counts: { submissions: 0, queued: 0, playing: 0 },
  recent_submissions: [],
  pending_submissions: [],
  queue: [],
  history: [],
  wall: [],
  failed_submissions: [],
  tables: [],
  playback: { state: 'stopped', current: null },
  diagnostics: { bot: 'loading', bot_error: null, video_worker: 'loading', video_worker_error: null, screen: 'loading', ffmpeg: 'loading', ffmpeg_path: null, ffmpeg_version: null, ffprobe: 'loading', ffprobe_path: null, ffprobe_version: null, database: '', free_disk_mb: null, min_free_disk_mb: 0, disk: 'loading' },
  screen: { status: 'loading', screen_id: null, last_seen_at: null, message: null, progress_sec: null, duration_sec: null, load_timeout_sec: 12 },
};

type SettingsDraft = {
  company_name: string;
  event_name: string;
  screen_text: string;
  volume: number;
  moderation_mode: 'manual' | 'auto';
  playback_mode: 'auto_loop' | 'interactive_wall';
  theme: 'new_year' | 'neutral';
  max_duration_sec: number;
  max_size_mb: number;
  guest_limit: number;
  transition_seconds: number;
  primary_color: string;
  accent_color: string;
};

function App() {
  const [state, setState] = useState<AppState>(emptyState);
  const [error, setError] = useState<string>('');
  const stateVersionRef = useRef(emptyState.version);
  const route = window.location.pathname === '/screen' ? 'screen' : 'admin';

  function applyState(nextState: AppState) {
    if (nextState.version < stateVersionRef.current) return;
    stateVersionRef.current = nextState.version;
    setState(nextState);
  }

  async function loadState() {
    const response = await fetch('/api/state');
    if (!response.ok) throw new Error('Не удалось получить состояние');
    applyState(await response.json());
  }

  useEffect(() => {
    let closedByApp = false;
    let reconnectTimer: number | null = null;
    let ws: WebSocket | null = null;
    loadState().catch((err: Error) => setError(err.message));

    function connectWs() {
      const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws';
      ws = new WebSocket(`${protocol}://${window.location.host}/api/ws`);
      ws.onopen = () => {
        loadState()
          .then(() => setError(''))
          .catch((err: Error) => setError(err.message));
      };
      ws.onmessage = (event) => {
        const message = JSON.parse(event.data);
        if (message.type === 'state') applyState(message.payload);
      };
      ws.onclose = () => {
        if (closedByApp) return;
        setError('Связь с сервером потеряна. Видео приостановлено до восстановления.');
        reconnectTimer = window.setTimeout(connectWs, 1500);
      };
      ws.onerror = () => ws?.close();
    }

    connectWs();
    return () => {
      closedByApp = true;
      if (reconnectTimer !== null) window.clearTimeout(reconnectTimer);
      ws?.close();
    };
  }, []);

  if (route === 'screen') return <Screen state={state} error={error} />;
  return <Admin state={state} error={error} reload={loadState} />;
}

function Admin({ state, error, reload }: { state: AppState; error: string; reload: () => Promise<void> }) {
  const isElectron = Boolean(window.desktop);
  const event = state.active_event;
  const queuedItems = state.queue.filter((item) => item.status === 'queued');
  const current = state.playback.current;
  const activeQueueSubmissionIds = new Set(state.queue.map((item) => item.submission_id));
  const wallSubmissionIds = new Set(state.wall.map((submission) => submission.id));
  const deleteBlockedSubmissionIds = new Set(current ? [current.submission_id] : []);
  const wallStartBlockedReason = current ? 'Сначала остановите или пропустите текущий ролик' : !state.screen.screen_id || state.screen.status === 'stale' || state.screen.status === 'not_connected' ? 'Сначала откройте экран показа' : null;
  const progress = state.screen.duration_sec ? Math.min(100, Math.round(((state.screen.progress_sec || 0) / state.screen.duration_sec) * 100)) : 0;
  const [companyName, setCompanyName] = useState('');
  const [eventName, setEventName] = useState('');
  const [screenText, setScreenText] = useState('С Новым годом!');
  const [message, setMessage] = useState('');
  const [pendingAction, setPendingAction] = useState<string | null>(null);
  const [settingsDraft, setSettingsDraft] = useState<SettingsDraft | null>(null);
  const [preview, setPreview] = useState<Submission | null>(null);
  const [adminQr, setAdminQr] = useState('');
  const [tableCount, setTableCount] = useState(1);
  const [tableTasks, setTableTasks] = useState<string[]>(['']);
  const [tableFilter, setTableFilter] = useState<number | 'all'>('all');
  const [desktopScreen, setDesktopScreen] = useState<DesktopScreenState | null>(null);
  const [desktopMessage, setDesktopMessage] = useState('');
  const [desktopAction, setDesktopAction] = useState<string | null>(null);
  const previewVideoRef = useRef<HTMLVideoElement | null>(null);

  useEffect(() => {
    if (!event) {
      setSettingsDraft(null);
      return;
    }
    setSettingsDraft(eventToDraft(event));
    setTableCount(event.table_count || 1);
    setTableTasks(Array.from({ length: event.table_count || 1 }, (_, index) => state.tables.find((table) => table.number === index + 1)?.task || ''));
    setTableFilter('all');
  }, [event?.id]);

  useEffect(() => {
    if (!event?.connection_link) {
      setAdminQr('');
      return;
    }
    QRCode.toDataURL(event.connection_link, { margin: 1, width: 180 }).then(setAdminQr).catch(() => setAdminQr(''));
  }, [event?.connection_link]);

  useEffect(() => {
    if (!window.desktop) return;
    let active = true;
    const applyState = (next: DesktopScreenState) => {
      if (!active) return;
      setDesktopScreen(next);
      if (next.warning) setDesktopMessage(next.warning);
    };
    window.desktop.getScreenState().then(applyState).catch(() => setDesktopMessage('Не удалось получить список мониторов.'));
    const unsubscribe = window.desktop.onScreenStateChanged(applyState);
    return () => {
      active = false;
      unsubscribe();
    };
  }, []);

  function closePreview() {
    if (previewVideoRef.current) {
      previewVideoRef.current.pause();
      previewVideoRef.current.removeAttribute('src');
      previewVideoRef.current.load();
    }
    setPreview(null);
  }

  async function createEvent(formEvent: React.FormEvent) {
    formEvent.preventDefault();
    await runAction('create-event', 'Мероприятие создано', async () => {
      const response = await fetch('/api/events', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ company_name: companyName, event_name: eventName, screen_text: screenText }),
      });
      if (!response.ok) throw new Error(await readError(response, 'Не удалось создать мероприятие'));
      setCompanyName('');
      setEventName('');
    });
  }

  async function runAction(key: string, success: string, action: () => Promise<string | void>) {
    if (pendingAction) return;
    setPendingAction(key);
    setMessage('');
    try {
      const actionMessage = await action();
      setMessage(actionMessage || success);
      await reload();
    } catch (err) {
      setMessage(err instanceof Error ? err.message : 'Команда не выполнена');
    } finally {
      setPendingAction(null);
    }
  }

  async function postAction(key: string, url: string, success: string, body?: unknown) {
    await runAction(key, success, async () => {
      const response = await fetch(url, {
        method: 'POST',
        headers: body ? { 'Content-Type': 'application/json' } : undefined,
        body: body ? JSON.stringify(body) : undefined,
      });
      if (!response.ok) throw new Error(await readError(response, 'Команда не выполнена'));
    });
  }

  async function patchActive(key: string, payload: Partial<Event>) {
    if (!event) return;
    await runAction(key, 'Сохранено', async () => {
      const response = await fetch(`/api/events/${event.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!response.ok) throw new Error(await readError(response, 'Не удалось сохранить изменения'));
    });
  }

  async function saveSettings(formEvent: React.FormEvent) {
    formEvent.preventDefault();
    if (!settingsDraft) return;
    await patchActive('save-settings', settingsDraft as Partial<Event>);
  }

  async function saveTables(formEvent: React.FormEvent) {
    formEvent.preventDefault();
    if (!event) return;
    await runAction('save-tables', 'Столы сохранены', async () => {
      const response = await fetch(`/api/events/${event.id}/tables`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ table_count: tableCount, tasks: tableTasks.slice(0, tableCount) }),
      });
      if (!response.ok) throw new Error(await readError(response, 'Не удалось сохранить столы'));
    });
  }

  function changeTableCount(value: number) {
    setTableCount(value);
    setTableTasks((tasks) => Array.from({ length: value }, (_, index) => tasks[index] || ''));
  }

  async function saveSubmissionTable(submission: Submission, tableNumber: number) {
    await runAction(`table-${submission.id}`, 'Стол ролика изменён', async () => {
      const response = await fetch(`/api/submissions/${submission.id}/table`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ table_number: tableNumber }),
      });
      if (!response.ok) throw new Error(await readError(response, 'Не удалось изменить стол ролика'));
    });
  }

  const visiblePending = tableFilter === 'all' ? state.pending_submissions : state.pending_submissions.filter((submission) => submission.table_number === tableFilter);
  const visibleRecent = tableFilter === 'all' ? state.recent_submissions : state.recent_submissions.filter((submission) => submission.table_number === tableFilter);

  async function activate(id: string) {
    if (event && event.id !== id && event.playback_state !== 'stopped') {
      setMessage('Сначала остановите показ, затем переключите мероприятие.');
      return;
    }
    await postAction(`activate-${id}`, `/api/events/${id}/activate`, 'Мероприятие выбрано');
  }

  async function saveDisplayName(submission: Submission, value: string) {
    await runAction(`name-${submission.id}`, 'Имя сохранено', async () => {
      const response = await fetch(`/api/submissions/${submission.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ guest_display_name: value }),
      });
      if (!response.ok) throw new Error(await readError(response, 'Не удалось сохранить имя'));
    });
  }

  async function deleteSubmission(submission: Submission) {
    if (current?.submission_id === submission.id) {
      setMessage('Нельзя удалить кружочек, который сейчас воспроизводится. Сначала остановите или пропустите показ.');
      return;
    }
    const confirmed = window.confirm('Точно удалить этот кружочек? Видео, очередь, история и файлы будут удалены без возможности восстановления.');
    if (!confirmed) return;
    await runAction(`delete-${submission.id}`, 'Кружочек удалён', async () => {
      const response = await fetch(`/api/submissions/${submission.id}`, { method: 'DELETE' });
      if (!response.ok) throw new Error(await readError(response, 'Не удалось удалить кружочек'));
    });
  }

  async function moveQueueItem(itemId: string, direction: -1 | 1) {
    if (!event) return;
    const ordered = queuedItems.map((item) => item.id);
    const index = ordered.indexOf(itemId);
    const target = index + direction;
    if (index === -1 || target < 0 || target >= ordered.length) return;
    [ordered[index], ordered[target]] = [ordered[target], ordered[index]];
    await postAction(`reorder-${itemId}`, `/api/events/${event.id}/queue/reorder`, 'Порядок очереди изменён', { queue_item_ids: ordered });
  }

  async function approveAllPending() {
    if (!event || !window.confirm('Одобрить все готовые ролики, ожидающие проверки?')) return;
    await postAction('approve-all', `/api/events/${event.id}/approve-all-pending`, 'Ожидающие ролики одобрены');
  }

  async function clearHistory() {
    if (!event) return;
    await runAction('clear-history', 'История очищена', async () => {
      const response = await fetch(`/api/events/${event.id}/history/clear`, { method: 'POST' });
      if (!response.ok) throw new Error(await readError(response, 'Не удалось очистить историю'));
      const result = await response.json() as { cleared: number };
      return result.cleared > 0 ? `История очищена: ${result.cleared} записей` : 'Нет записей для очистки';
    });
  }

  async function uploadBackground(file: File | null) {
    if (!event || !file) return;
    await runAction('background', 'Фон загружен', async () => {
      const data = new FormData();
      data.append('file', file);
      const response = await fetch(`/api/events/${event.id}/background`, { method: 'POST', body: data });
      if (!response.ok) throw new Error(await readError(response, 'Не удалось загрузить фон'));
    });
  }

  async function startWallSubmission(submission: Submission) {
    await postAction(`wall-start-${submission.id}`, `/api/submissions/${submission.id}/admin-play-now`, 'Ролик отправлен на экран');
  }

  async function runDesktopAction(key: string, action: () => Promise<DesktopScreenState>) {
    if (!window.desktop || desktopAction) return;
    setDesktopAction(key);
    setDesktopMessage('');
    try {
      const next = await action();
      setDesktopScreen(next);
      setDesktopMessage(next.warning || '');
    } catch (err) {
      setDesktopMessage(err instanceof Error ? err.message : 'Не удалось выполнить команду окна показа.');
    } finally {
      setDesktopAction(null);
    }
  }

  async function openConfiguration() {
    if (!window.desktop || desktopAction) return;
    setDesktopAction('open-configuration');
    setDesktopMessage('');
    try {
      await window.desktop.openConfiguration();
      setDesktopMessage('Открыта папка конфигурации. Укажите TELEGRAM_BOT_TOKEN и TELEGRAM_BOT_USERNAME в файле .env, затем перезапустите приложение.');
    } catch (err) {
      setDesktopMessage(err instanceof Error ? err.message : 'Не удалось открыть папку конфигурации.');
    } finally {
      setDesktopAction(null);
    }
  }

  return (
    <main className="admin-shell">
      <header className="topbar">
        <div>
          <p className="eyebrow">Этап 9 · панель оператора</p>
          <h1>Панель оператора</h1>
        </div>
        {isElectron ? <button className="screen-link" disabled={!!desktopAction} type="button" onClick={() => runDesktopAction('open-screen', () => window.desktop!.openScreen())}>{desktopAction === 'open-screen' ? 'Открываем...' : 'Открыть экран'}</button> : <a className="screen-link" href="/screen" target="_blank" rel="noreferrer">Открыть в браузере</a>}
      </header>

      {error && <div className="notice error">{error}</div>}
      {message && <div className={message.includes('не') || message.includes('Сначала') ? 'notice error' : 'notice'}>{message}</div>}

      <section className="grid">
        <section className="card wide">
          <div className="section-heading">
            <div>
              <h2>Мероприятие</h2>
              <p className="muted">Создание, выбор, приём гостей и ссылка подключения.</p>
            </div>
            {event && <button type="button" disabled={!!pendingAction} onClick={() => patchActive('reception', { reception_open: !event.reception_open })}>{event.reception_open ? 'Закрыть приём' : 'Открыть приём'}</button>}
          </div>
          {event ? (
            <div className="event-summary">
              <div><span>Компания</span><strong>{event.company_name}</strong></div>
              <div><span>Текст экрана</span><strong>{event.screen_text}</strong></div>
              <div><span>Приём</span><strong>{event.reception_open ? 'Открыт' : 'Закрыт'}</strong></div>
              <div><span>Код</span><code>{event.code}</code></div>
              <div><span>Ссылка Telegram</span>{event.connection_link ? <a href={event.connection_link}>{event.connection_link}</a> : <strong>Укажите TELEGRAM_BOT_USERNAME</strong>}</div>
              <div><span>QR-код</span>{adminQr ? <img className="admin-qr" src={adminQr} alt="QR-код подключения" /> : <strong>Нет ссылки</strong>}</div>
            </div>
          ) : <p className="muted">Создайте первое мероприятие.</p>}
        </section>

        <form className="card" onSubmit={createEvent}>
          <h2>Создать мероприятие</h2>
          <label>Название компании<input value={companyName} onChange={(e) => setCompanyName(e.target.value)} required placeholder="Например, ACME" /></label>
          <label>Название мероприятия<input value={eventName} onChange={(e) => setEventName(e.target.value)} placeholder="Новогодний вечер" /></label>
          <label>Текст экрана<input value={screenText} onChange={(e) => setScreenText(e.target.value)} /></label>
          <button disabled={!!pendingAction}>{pendingAction === 'create-event' ? 'Создаём...' : 'Создать'}</button>
        </form>

        <div className="card">
          <h2>Сохранённые мероприятия</h2>
          <div className="event-list">
            {state.events.map((item) => <button className={item.active ? 'event-item active' : 'event-item'} disabled={!!pendingAction || item.active} key={item.id} onClick={() => activate(item.id)}><span>{item.company_name}</span><small>{item.active ? 'активно' : 'выбрать'}</small></button>)}
            {state.events.length === 0 && <p className="muted">Пока пусто.</p>}
          </div>
        </div>

        <section className="card wide screen-control">
          <div className="section-heading">
            <div>
              <h2>Экран показа</h2>
              <p className="muted">{isElectron ? 'Выберите монитор Windows и управляйте единственным полноэкранным окном трансляции.' : 'Вы открыли browser-режим. Выбор монитора и полноэкранное окно доступны только в Visual Pozdr Desktop.'}</p>
            </div>
            {desktopScreen && <strong className={desktopScreen.screenOpen ? 'screen-status open' : 'screen-status'}>{desktopScreen.screenOpen ? 'Открыт' : 'Закрыт'}</strong>}
          </div>
          {desktopScreen ? <>
            {desktopMessage && <p className="desktop-message">{desktopMessage}</p>}
            <label>Монитор для показа
              <select value={desktopScreen.selectedDisplayId} disabled={!!desktopAction} onChange={(e) => runDesktopAction('select-display', () => window.desktop!.selectScreenDisplay(e.target.value))}>
                {desktopScreen.displays.map((display) => <option key={display.id} value={display.id}>{display.name} · {display.workArea.width}×{display.workArea.height}{display.primary ? ' · основной' : ''}</option>)}
              </select>
            </label>
            <div className="display-list">
              {desktopScreen.displays.map((display) => <div className={display.id === desktopScreen.selectedDisplayId ? 'display-item selected' : 'display-item'} key={display.id}>
                <strong>{display.name}{display.primary ? ' · основной' : ''}</strong>
                <span>Разрешение: {display.bounds.width}×{display.bounds.height}</span>
                <span>Рабочая область: {display.workArea.width}×{display.workArea.height}</span>
                <span>Масштаб: {Math.round(display.scaleFactor * 100)}%</span>
              </div>)}
            </div>
            <div className="button-row">
              <button disabled={!!desktopAction} type="button" onClick={() => runDesktopAction('open-screen', () => window.desktop!.openScreen())}>{desktopAction === 'open-screen' ? 'Открываем...' : 'Открыть экран'}</button>
              <button disabled={!!desktopAction || !desktopScreen.screenOpen} type="button" onClick={() => runDesktopAction('move-screen', () => window.desktop!.moveScreen())}>Переместить экран</button>
              <button disabled={!!desktopAction || !desktopScreen.screenOpen} type="button" className="danger" onClick={() => runDesktopAction('close-screen', () => window.desktop!.closeScreen())}>Закрыть экран</button>
              <button disabled={!!desktopAction} type="button" onClick={openConfiguration}>{desktopAction === 'open-configuration' ? 'Открываем...' : 'Открыть конфигурацию'}</button>
            </div>
          </> : isElectron ? <p className="muted">Получаем список мониторов Windows...</p> : <div className="desktop-required"><strong>Запущен browser-режим</strong><span>Закройте браузер и запустите desktop-оболочку: <code>cd frontend</code>, затем <code>npm run desktop:dev</code>. Для установленной версии откройте Visual Pozdr из меню Windows.</span></div>}
        </section>

        <section className="card wide">
          <h2>Сейчас на экране</h2>
          <div className="now-playing">
            <div className="submission-preview large">{current?.submission.thumbnail_path ? <img src={mediaUrl(current.submission.thumbnail_path)} alt="Превью текущего ролика" /> : <span>Нет ролика</span>}</div>
            <div>
              <strong>{current?.submission.guest_display_name || 'Нет активного ролика'}</strong>
              {current && <TableLabel submission={current.submission} />}
              <div className="playback-panel compact-panel">
                <div><span>Показ</span><strong>{state.playback.state}</strong></div>
                <div><span>Экран</span><strong>{state.screen.status}</strong></div>
                <div><span>Прогресс</span><strong>{formatProgress(state.screen.progress_sec, state.screen.duration_sec)}</strong></div>
              </div>
              <div className="progress"><span style={{ width: `${progress}%` }} /></div>
              {state.screen.status === 'autoplay_blocked' && <p className="screen-warning">Браузер заблокировал запуск со звуком. Нажмите «Запустить показ» во вкладке экрана показа.</p>}
              <div className="button-row">
                {event && <button disabled={!!pendingAction} type="button" onClick={() => postAction('start', `/api/events/${event.id}/playback/start`, 'Показ запущен')}>Запустить</button>}
                {event && <button disabled={!!pendingAction || !current} type="button" onClick={() => postAction('pause', `/api/events/${event.id}/playback/pause`, 'Показ приостановлен')}>Пауза</button>}
                {event && <button disabled={!!pendingAction || !current} type="button" onClick={() => postAction('resume', `/api/events/${event.id}/playback/resume`, 'Показ продолжен')}>Продолжить</button>}
                {event && <button disabled={!!pendingAction || !current} type="button" onClick={() => postAction('skip', `/api/events/${event.id}/playback/skip`, 'Ролик пропущен', { playback_id: current?.playback_id })}>Пропустить</button>}
                {event && <button disabled={!!pendingAction} type="button" className="danger" onClick={() => postAction('stop', `/api/events/${event.id}/playback/stop`, 'Показ остановлен')}>Остановить показ</button>}
              </div>
            </div>
          </div>
        </section>

        <section className="card wide">
          <div className="section-heading"><div><h2>На проверке</h2><p className="muted">Предпросмотр открывается без звука и останавливается при закрытии.</p></div>{state.pending_submissions.length > 0 && <button disabled={!!pendingAction} type="button" onClick={approveAllPending}>Одобрить все ожидающие</button>}</div>
          <TableFilter tables={state.tables} value={tableFilter} onChange={setTableFilter} />
          <SubmissionCards submissions={visiblePending} pendingAction={pendingAction} tables={state.tables} onTable={saveSubmissionTable} onPreview={setPreview} onName={saveDisplayName} onApprove={(id) => postAction(`approve-${id}`, `/api/submissions/${id}/approve`, 'Ролик одобрен')} onReject={(id) => postAction(`reject-${id}`, `/api/submissions/${id}/reject`, 'Ролик отклонён')} />
        </section>

        <section className="card wide">
          <h2>Очередь</h2>
          {current && <article className="queue-item current"><strong>Сейчас: {current.submission.guest_display_name || `Telegram user ${current.submission.telegram_user_id}`}</strong><TableLabel submission={current.submission} /><code>{current.is_loop ? 'повтор' : current.status}</code>{current.submission.thumbnail_path && <img src={mediaUrl(current.submission.thumbnail_path)} alt="Превью" />}</article>}
          {queuedItems.length > 0 ? <div className="queue-list">{queuedItems.map((item) => <article className="queue-item" key={item.id}><strong>{item.position}. {item.submission.guest_display_name || `Telegram user ${item.submission.telegram_user_id}`}</strong><TableLabel submission={item.submission} /><code>{item.is_loop ? 'повтор' : item.status}</code>{item.submission.thumbnail_path && <img src={mediaUrl(item.submission.thumbnail_path)} alt="Превью" />}<div className="button-row"><button disabled={!!pendingAction} type="button" onClick={() => moveQueueItem(item.id, -1)}>Выше</button><button disabled={!!pendingAction} type="button" onClick={() => moveQueueItem(item.id, 1)}>Ниже</button><button disabled={!!pendingAction} type="button" className="danger" onClick={() => postAction(`cancel-${item.id}`, `/api/queue/${item.id}/cancel`, 'Элемент убран из очереди')}>Убрать</button><button disabled={!!pendingAction} type="button" className="danger" onClick={() => deleteSubmission(item.submission)}>Удалить полностью</button></div></article>)}</div> : <p className="muted">Ожидающая очередь пуста.</p>}
        </section>

        <section className="card wide">
          <div className="section-heading"><div><h2>История</h2><p className="muted">Очистка скрывает журнал показов, но не удаляет ролики, очередь и стену.</p></div>{event && state.history.length > 0 && <button disabled={!!pendingAction} type="button" className="danger" onClick={clearHistory}>Очистить историю</button>}</div>
          {state.history.length > 0 ? <div className="history-list">{state.history.map((item) => <article className="history-item" key={item.id}><div><strong>{item.submission.guest_display_name || `Telegram user ${item.submission.telegram_user_id}`}</strong><TableLabel submission={item.submission} /><span>{formatDate(item.updated_at)}</span></div><code>{item.status}</code>{item.error_reason && <small className="error-text">{item.error_reason}</small>}<div className="button-row"><button disabled={!!pendingAction || activeQueueSubmissionIds.has(item.submission_id)} type="button" onClick={() => postAction(`replay-${item.submission_id}`, `/api/submissions/${item.submission_id}/replay`, 'Повтор добавлен в очередь')}>{activeQueueSubmissionIds.has(item.submission_id) ? 'Уже в очереди' : 'Показать повторно'}</button><button disabled={!!pendingAction || wallSubmissionIds.has(item.submission_id) || state.wall.length >= 12} type="button" onClick={() => postAction(`wall-add-${item.submission_id}`, `/api/submissions/${item.submission_id}/wall`, 'Добавлено на стену', { visible: true })}>{wallSubmissionIds.has(item.submission_id) ? 'Уже на стене' : 'Добавить на стену'}</button><button disabled={!!pendingAction || current?.submission_id === item.submission_id} type="button" className="danger" onClick={() => deleteSubmission(item.submission)}>Удалить полностью</button></div></article>)}</div> : <p className="muted">История показов пока пустая.</p>}
        </section>

        <section className="card wide">
          <h2>Стена</h2>
          <p className="muted">Стена управляется оператором и вмещает максимум 12 кружочков. Запуск не прерывает уже идущий ролик.</p>
          {wallStartBlockedReason && <p className="muted">{wallStartBlockedReason}</p>}
          {state.wall.length > 0 ? <div className="admin-wall-list">{state.wall.map((submission) => <article className="admin-wall-item" key={submission.id}>{submission.thumbnail_path ? <img src={mediaUrl(submission.thumbnail_path)} alt="Превью на стене" /> : <span>Нет превью</span>}<div><strong>{submission.guest_display_name || `Telegram user ${submission.telegram_user_id}`}</strong><TableLabel submission={submission} /><small>{submission.wall_completed_at ? formatDate(submission.wall_completed_at) : formatDate(submission.updated_at)}</small></div><div className="button-row"><button disabled={!!pendingAction} type="button" className="danger" onClick={() => postAction(`wall-hide-${submission.id}`, `/api/submissions/${submission.id}/wall`, 'Убрано со стены', { visible: false })}>Убрать со стены</button>{wallStartBlockedReason ? <span className="disabled-action" title={wallStartBlockedReason}><button disabled type="button">Запустить</button></span> : <button disabled={!!pendingAction} type="button" onClick={() => startWallSubmission(submission)}>{pendingAction === `wall-start-${submission.id}` ? 'Запускаем...' : 'Запустить'}</button>}</div></article>)}</div> : <p className="muted">Стена появится после завершения первого ролика.</p>}
        </section>

        <section className="card wide">
          <h2>Ошибки подготовки</h2>
          {state.failed_submissions.length > 0 ? <div className="history-list">{state.failed_submissions.map((submission) => <article className="history-item" key={submission.id}><div><strong>{submission.guest_display_name || `Telegram user ${submission.telegram_user_id}`}</strong><TableLabel submission={submission} /><small className="error-text">{submission.error_reason}</small></div><div className="button-row"><button disabled={!!pendingAction} type="button" onClick={() => postAction(`retry-${submission.id}`, `/api/submissions/${submission.id}/retry`, 'Отправлено на повторную обработку')}>Повторить обработку</button><button disabled={!!pendingAction} type="button" className="danger" onClick={() => deleteSubmission(submission)}>Удалить полностью</button></div></article>)}</div> : <p className="muted">Нет технически ошибочных роликов.</p>}
        </section>

        <section className="card wide">
          <h2>Принятые кружочки</h2>
          <TableFilter tables={state.tables} value={tableFilter} onChange={setTableFilter} />
          <SubmissionCards submissions={visibleRecent} pendingAction={pendingAction} tables={state.tables} onTable={saveSubmissionTable} wallSubmissionIds={wallSubmissionIds} deleteBlockedSubmissionIds={deleteBlockedSubmissionIds} wallFull={state.wall.length >= 12} onPreview={setPreview} onName={saveDisplayName} onReplay={(id) => postAction(`replay-${id}`, `/api/submissions/${id}/replay`, 'Повтор добавлен в очередь')} onWall={(id) => postAction(`wall-add-${id}`, `/api/submissions/${id}/wall`, 'Добавлено на стену', { visible: true })} onRetry={(id) => postAction(`retry-${id}`, `/api/submissions/${id}/retry`, 'Отправлено на повторную обработку')} onDelete={deleteSubmission} />
        </section>

        <form className="card wide" onSubmit={saveSettings}>
          <h2>Настройки</h2>
          {settingsDraft && <div className="settings-grid"><label>Режим показа<select value={settingsDraft.playback_mode} onChange={(e) => setSettingsDraft({ ...settingsDraft, playback_mode: e.target.value as 'auto_loop' | 'interactive_wall' })}><option value="auto_loop">Автопоказ</option><option value="interactive_wall">Интерактивная стена</option></select></label><label>Тема<select value={settingsDraft.theme} onChange={(e) => setSettingsDraft({ ...settingsDraft, theme: e.target.value as 'new_year' | 'neutral' })}><option value="new_year">Новогоднее настроение</option><option value="neutral">Нейтральный</option></select></label></div>}
          {settingsDraft ? <div className="settings-grid"><label>Компания<input value={settingsDraft.company_name} onChange={(e) => setSettingsDraft({ ...settingsDraft, company_name: e.target.value })} /></label><label>Мероприятие<input value={settingsDraft.event_name} onChange={(e) => setSettingsDraft({ ...settingsDraft, event_name: e.target.value })} /></label><label>Текст экрана<input value={settingsDraft.screen_text} onChange={(e) => setSettingsDraft({ ...settingsDraft, screen_text: e.target.value })} /></label><label>Модерация<select value={settingsDraft.moderation_mode} onChange={(e) => setSettingsDraft({ ...settingsDraft, moderation_mode: e.target.value as 'manual' | 'auto' })}><option value="manual">Ручная</option><option value="auto">Автоматическая</option></select></label><label>Громкость: {settingsDraft.volume}%<input type="range" min="0" max="100" value={settingsDraft.volume} onChange={(e) => setSettingsDraft({ ...settingsDraft, volume: Number(e.target.value) })} /></label><label>Макс. длительность, сек<input type="number" min="1" value={settingsDraft.max_duration_sec} onChange={(e) => setSettingsDraft({ ...settingsDraft, max_duration_sec: Number(e.target.value) })} /></label><label>Макс. размер, МБ<input type="number" min="1" value={settingsDraft.max_size_mb} onChange={(e) => setSettingsDraft({ ...settingsDraft, max_size_mb: Number(e.target.value) })} /></label><label>Лимит на гостя<input type="number" min="1" value={settingsDraft.guest_limit} onChange={(e) => setSettingsDraft({ ...settingsDraft, guest_limit: Number(e.target.value) })} /></label><label>Основной цвет<input type="color" value={settingsDraft.primary_color} onChange={(e) => setSettingsDraft({ ...settingsDraft, primary_color: e.target.value })} /></label><label>Акцент<input type="color" value={settingsDraft.accent_color} onChange={(e) => setSettingsDraft({ ...settingsDraft, accent_color: e.target.value })} /></label><label>Фоновое изображение<input type="file" accept="image/png,image/jpeg,image/webp" onChange={(e) => uploadBackground(e.target.files?.[0] || null)} /><small className="muted">Рекомендуется 1920x1080 или 2560x1440. Важные детали держите по центру: фон показывается в режиме cover, края могут обрезаться.</small></label><button disabled={!!pendingAction}>{pendingAction === 'save-settings' ? 'Сохраняем...' : 'Сохранить настройки'}</button></div> : <p className="muted">Создайте мероприятие.</p>}
        </form>

        <form className="card wide" onSubmit={saveTables}>
          <div className="section-heading"><div><h2>Столы и задания</h2><p className="muted">Бот спросит номер стола до первого кружочка. Задание показывается только оператору и на экране ролика.</p></div></div>
          <label>Количество столов<select value={tableCount} onChange={(event) => changeTableCount(Number(event.target.value))}>{Array.from({ length: 15 }, (_, index) => <option key={index + 1} value={index + 1}>{index + 1}</option>)}</select></label>
          <div className="table-tasks">{tableTasks.slice(0, tableCount).map((task, index) => <label key={index}>Стол {index + 1}: задание<input value={task} maxLength={240} onChange={(event) => setTableTasks(tableTasks.map((value, taskIndex) => taskIndex === index ? event.target.value : value))} placeholder="Необязательно" /></label>)}</div>
          <button disabled={!!pendingAction}>{pendingAction === 'save-tables' ? 'Сохраняем...' : 'Сохранить столы'}</button>
        </form>

        <section className="card wide">
          <h2>Диагностика</h2>
          <div className="diagnostics"><code>bot: {state.diagnostics.bot}</code><code>bot_error: {state.diagnostics.bot_error || 'none'}</code><code>video_worker: {state.diagnostics.video_worker}</code><code>ffmpeg: {state.diagnostics.ffmpeg}</code><code>ffmpeg_version: {state.diagnostics.ffmpeg_version || 'unknown'}</code><code>ffmpeg_path: {state.diagnostics.ffmpeg_path || 'missing'}</code><code>ffprobe: {state.diagnostics.ffprobe}</code><code>ffprobe_version: {state.diagnostics.ffprobe_version || 'unknown'}</code><code>ffprobe_path: {state.diagnostics.ffprobe_path || 'missing'}</code><code>screen: {state.diagnostics.screen}</code><code>screen_message: {state.screen.message || 'none'}</code><code>disk: {state.diagnostics.disk}</code><code>free_disk_mb: {state.diagnostics.free_disk_mb ?? 'unknown'} / {state.diagnostics.min_free_disk_mb}</code><code>database: {state.diagnostics.database}</code></div>
          {state.diagnostics.video_worker_error && <p className="error-text">{state.diagnostics.video_worker_error}</p>}
        </section>
      </section>

      {preview && <div className="modal-backdrop"><div className="preview-modal"><div className="section-heading"><h2>{preview.guest_display_name || 'Предпросмотр'}</h2><button type="button" onClick={closePreview}>Закрыть</button></div>{preview.playback_path && <video ref={previewVideoRef} src={mediaUrl(preview.playback_path)} muted controls autoPlay preload="metadata" />}</div></div>}
    </main>
  );
}

function SubmissionCards({
  submissions,
  pendingAction,
  onPreview,
  onName,
  onApprove,
  onReject,
  onReplay,
  onWall,
  onRetry,
  onDelete,
  tables = [],
  onTable,
  wallSubmissionIds = new Set<string>(),
  deleteBlockedSubmissionIds = new Set<string>(),
  wallFull = false,
}: {
  submissions: Submission[];
  pendingAction: string | null;
  onPreview: (submission: Submission) => void;
  onName: (submission: Submission, value: string) => void;
  onApprove?: (id: string) => void;
  onReject?: (id: string) => void;
  onReplay?: (id: string) => void;
  onWall?: (id: string) => void;
  onRetry?: (id: string) => void;
  onDelete?: (submission: Submission) => void;
  tables?: EventTable[];
  onTable?: (submission: Submission, tableNumber: number) => void;
  wallSubmissionIds?: Set<string>;
  deleteBlockedSubmissionIds?: Set<string>;
  wallFull?: boolean;
}) {
  if (submissions.length === 0) return <p className="muted">Пока пусто.</p>;
  return (
    <div className="submission-list stage6-list">
      {submissions.map((submission) => (
        <article className="submission-item" key={submission.id}>
          {onDelete && <button className="submission-delete" disabled={!!pendingAction || deleteBlockedSubmissionIds.has(submission.id)} title={deleteBlockedSubmissionIds.has(submission.id) ? 'Нельзя удалить ролик, который сейчас воспроизводится' : 'Удалить кружочек полностью'} type="button" onClick={() => onDelete(submission)} aria-label="Удалить кружочек">×</button>}
          <div className="submission-preview">
            {submission.thumbnail_path ? <img src={mediaUrl(submission.thumbnail_path)} alt="Превью кружочка" /> : <span>Нет превью</span>}
          </div>
          <div>
            <label>
              Имя гостя
              <input defaultValue={submission.guest_display_name} onBlur={(event) => onName(submission, event.target.value)} />
            </label>
            <span>{formatDate(submission.created_at)}</span>
            <TableLabel submission={submission} />
            {onTable && tables.length > 0 && <label>Стол<select value={submission.table_number || ''} onChange={(event) => event.target.value && onTable(submission, Number(event.target.value))}><option value="" disabled>Не указан</option>{tables.map((table) => <option key={table.number} value={table.number}>Стол {table.number}</option>)}</select></label>}
          </div>
          <div>
            <span>Статусы</span>
            <code className={`status status-${submission.preparation_status}`}>{submission.preparation_status}</code>
            <code className="status">{submission.moderation_status}</code>
            {submission.error_reason && <small className="error-text">{submission.error_reason}</small>}
          </div>
          <div>
            <span>Длительность</span>
            <strong>{submission.duration_sec ? `${Math.round(submission.duration_sec)} сек` : 'нет данных'}</strong>
          </div>
          <div>
            <span>Размер</span>
            <strong>{formatBytes(submission.telegram_file_size)}</strong>
          </div>
          <div className="submission-actions">
            {submission.playback_path && <button disabled={!!pendingAction} type="button" onClick={() => onPreview(submission)}>Предпросмотр</button>}
            {onApprove && <button disabled={!!pendingAction} type="button" onClick={() => onApprove(submission.id)}>Одобрить</button>}
            {onReject && <button disabled={!!pendingAction} type="button" className="danger" onClick={() => onReject(submission.id)}>Отклонить</button>}
            {onReplay && submission.preparation_status === 'ready' && submission.moderation_status === 'approved' && <button disabled={!!pendingAction} type="button" onClick={() => onReplay(submission.id)}>Показать повторно</button>}
            {onWall && submission.preparation_status === 'ready' && submission.moderation_status === 'approved' && <button disabled={!!pendingAction || wallSubmissionIds.has(submission.id) || wallFull} type="button" onClick={() => onWall(submission.id)}>{wallSubmissionIds.has(submission.id) ? 'Уже на стене' : 'Добавить на стену'}</button>}
            {onRetry && submission.preparation_status === 'failed' && <button disabled={!!pendingAction} type="button" onClick={() => onRetry(submission.id)}>Повторить обработку</button>}
          </div>
        </article>
      ))}
    </div>
  );
}

function TableLabel({ submission }: { submission: Submission }) {
  return submission.table_number ? <span className="table-label">Стол {submission.table_number}</span> : <span className="table-label missing">Стол не указан</span>;
}

function TableFilter({ tables, value, onChange }: { tables: EventTable[]; value: number | 'all'; onChange: (value: number | 'all') => void }) {
  if (tables.length === 0) return null;
  return <label className="table-filter">Фильтр по столу<select value={value} onChange={(event) => onChange(event.target.value === 'all' ? 'all' : Number(event.target.value))}><option value="all">Все столы</option>{tables.map((table) => <option key={table.number} value={table.number}>Стол {table.number}</option>)}</select></label>;
}

function eventToDraft(event: Event): SettingsDraft {
  return {
    company_name: event.company_name,
    event_name: event.event_name,
    screen_text: event.screen_text,
    volume: event.volume,
    moderation_mode: event.moderation_mode,
    playback_mode: event.playback_mode,
    theme: normalizeTheme(event.theme),
    max_duration_sec: event.max_duration_sec,
    max_size_mb: event.max_size_mb,
    guest_limit: event.guest_limit,
    transition_seconds: event.transition_seconds,
    primary_color: event.primary_color || '#102033',
    accent_color: event.accent_color || '#0f5f5c',
  };
}

async function readError(response: Response, fallback: string) {
  try {
    const data = await response.json();
    return typeof data.detail === 'string' ? data.detail : fallback;
  } catch {
    return fallback;
  }
}

function formatProgress(progress: number | null, duration: number | null) {
  if (progress == null || duration == null || duration <= 0) return 'нет данных';
  return `${Math.floor(progress)} / ${Math.ceil(duration)} сек`;
}

type ScreenThemeName = 'new_year' | 'neutral';
type WallSlot = { x: number; y: number; s: number };

const newYearSlots: WallSlot[] = [
  { x: 52, y: 14, s: 0.54 }, { x: 44, y: 27, s: 0.56 }, { x: 60, y: 28, s: 0.56 }, { x: 38, y: 42, s: 0.54 },
  { x: 52, y: 43, s: 0.6 }, { x: 66, y: 44, s: 0.54 }, { x: 32, y: 60, s: 0.5 }, { x: 45, y: 61, s: 0.56 },
  { x: 59, y: 62, s: 0.56 }, { x: 72, y: 63, s: 0.5 }, { x: 42, y: 80, s: 0.52 }, { x: 62, y: 81, s: 0.52 },
];

const neutralSlots: WallSlot[] = [
  { x: 16.67, y: 12.5, s: 1 }, { x: 50, y: 12.5, s: 1 }, { x: 83.33, y: 12.5, s: 1 },
  { x: 16.67, y: 37.5, s: 1 }, { x: 50, y: 37.5, s: 1 }, { x: 83.33, y: 37.5, s: 1 },
  { x: 16.67, y: 62.5, s: 1 }, { x: 50, y: 62.5, s: 1 }, { x: 83.33, y: 62.5, s: 1 },
  { x: 16.67, y: 87.5, s: 1 }, { x: 50, y: 87.5, s: 1 }, { x: 83.33, y: 87.5, s: 1 },
];

const screenThemes = {
  new_year: {
    label: 'Новогоднее настроение',
    assets: {
      background: '/themes/new_year/background.png',
      ornament: '/themes/new_year/ornament.png',
      tree: '/themes/new_year/tree-overlay.png',
    },
    positions: newYearSlots,
  },
  neutral: {
    label: 'Нейтральный',
    assets: {
      background: '/themes/neutral/background.png',
      frame: '/themes/neutral/circle-frame.png',
      zone: '/themes/neutral/zone-overlay.png',
    },
    positions: neutralSlots,
  },
};

function normalizeTheme(theme: string | null | undefined): ScreenThemeName {
  return theme === 'neutral' ? 'neutral' : 'new_year';
}

type ScreenThemeProps = {
  event: Event | null;
  state: AppState;
  current: QueueItem | null;
  currentVideoUrl: string;
  currentPlaybackId: string | null;
  autoplayBlocked: boolean;
  error: string;
  playbackNotice: string;
  screenMessage: string;
  qrDataUrl: string;
  videoRef: React.RefObject<HTMLVideoElement | null>;
  onStartShow: () => void;
  onPlayWallSubmission: (submission: Submission) => void;
  onEnded: () => void;
  onReplayInteractiveVideo: () => void;
  onCloseInteractiveVideo: () => void;
  onVideoError: () => void;
};

function ThemeRenderer(props: ScreenThemeProps) {
  const theme = normalizeTheme(props.event?.theme);
  const { event, state, current, currentVideoUrl, error } = props;
  const interactive = event?.playback_mode === 'interactive_wall';
  const isNeutral = theme === 'neutral';
  return (
    <main className={isNeutral ? (current ? 'screen-shell theme-neutral screen-playing' : 'screen-shell theme-neutral') : (current ? 'theme-screen theme-new-year is-playing' : 'theme-screen theme-new-year is-idle')}>
      {isNeutral ? <NeutralBackground /> : <NewYearBackground />}
      {error && <div className={isNeutral ? 'notice error' : 'notice error screen-notice'}>{error}</div>}
      {current && currentVideoUrl ? <PlayingScene {...props} /> : <IdleScene {...props} />}
      {isNeutral
        ? <CircleWall submissions={state.wall} compact={!!current} interactive={interactive} onPlayWallSubmission={props.onPlayWallSubmission} />
        : <OrnamentWall submissions={state.wall} compact={!!current} interactive={interactive} onPlayWallSubmission={props.onPlayWallSubmission} />}
    </main>
  );
}

function NeutralBackground() {
  return (
    <div className="neutral-background" aria-hidden="true">
      <img className="neutral-bg-image" src={screenThemes.neutral.assets.background} alt="" />
      <img className="neutral-zone" src={screenThemes.neutral.assets.zone} alt="" />
      <div className="neutral-vignette" />
    </div>
  );
}

function NewYearBackground() {
  return (
    <div className="new-year-background" aria-hidden="true">
      <img className="new-year-bg-image" src={screenThemes.new_year.assets.background} alt="" />
      <img className="new-year-tree" src={screenThemes.new_year.assets.tree} alt="" />
      <div className="new-year-vignette" />
    </div>
  );
}

function IdleScene(props: ScreenThemeProps) {
  const { event, qrDataUrl, playbackNotice, screenMessage, onStartShow } = props;
  const interactive = event?.playback_mode === 'interactive_wall';
  return (
    <div className="screen-card theme-idle-card">
      <p className="eyebrow">Экран трансляции · {interactive ? 'интерактивная стена' : 'ожидание'}</p>
      <h1>{event?.company_name || 'Visual Pozdr'}</h1>
      {event?.event_name?.trim() && <p className="screen-event-name">{event.event_name}</p>}
      <div className="screen-invite">
        <div className="qr-placeholder">
          {qrDataUrl ? <img src={qrDataUrl} alt="QR-код подключения к Telegram" /> : <span>QR</span>}
        </div>
        <div>
          <p className="screen-text screen-greeting">{event?.screen_text || (event ? '' : 'Создайте мероприятие в панели оператора')}</p>
          <strong>Отправьте видеопоздравление в Telegram</strong>
          <p className="screen-hint">Откройте камеру телефона, наведите на QR-код и отправьте видеокружочек.</p>
        </div>
      </div>
      {interactive ? <p className="screen-hint">Выберите кружочек здесь или нажмите «Запустить» у ролика на стене в панели оператора.</p> : <button className="launch-button" type="button" onClick={onStartShow}>Запустить показ</button>}
      {playbackNotice && <p className="screen-hint">{playbackNotice}</p>}
      {screenMessage && <p className="screen-warning">{screenMessage}</p>}
    </div>
  );
}

function PlayingScene(props: ScreenThemeProps) {
  const { current, event, autoplayBlocked, playbackNotice, screenMessage, state, onStartShow, onReplayInteractiveVideo, onCloseInteractiveVideo } = props;
  const interactive = event?.playback_mode === 'interactive_wall';
  return (
    <MainVideoArea {...props}>
      {interactive && <div className="interactive-player-actions"><button className="close-player-button" type="button" onClick={onCloseInteractiveVideo} aria-label="Закрыть видео" title="Закрыть видео">×</button><button className="replay-player-button" type="button" onClick={onReplayInteractiveVideo}>Повторить видео</button></div>}
      <p className="eyebrow">Сейчас на экране</p>
      <h1>{current?.submission.guest_display_name || `Гость ${current?.submission.telegram_user_id}`}</h1>
      <p className="screen-text compact table-screen-text">{current?.submission.table_number ? `Стол ${current.submission.table_number}${current.submission.table_task ? `\nЗадание: ${current.submission.table_task}` : ''}` : event?.screen_text || ''}</p>
      {autoplayBlocked && <button className="launch-button" type="button" onClick={onStartShow}>Запустить показ</button>}
      {playbackNotice && <p className="screen-hint">{playbackNotice}</p>}
      {screenMessage && <p className="screen-warning">{screenMessage}</p>}
      {state.playback.state === 'paused' && <p className="screen-warning">Показ приостановлен</p>}
    </MainVideoArea>
  );
}

function MainVideoArea({ currentPlaybackId, videoRef, onEnded, onVideoError, children }: ScreenThemeProps & { children: React.ReactNode }) {
  return (
    <section className="player-stage theme-player-stage">
      <div className="video-circle theme-video-circle">
        <video
          key={currentPlaybackId || 'empty-video'}
          ref={videoRef}
          playsInline
          preload="auto"
          onEnded={onEnded}
          onError={onVideoError}
        />
      </div>
      {children}
    </section>
  );
}

function OrnamentWall({ submissions, queueItems = [], compact = false, neutral = false, interactive = false, onPlayWallSubmission }: { submissions: Submission[]; queueItems?: QueueItem[]; compact?: boolean; neutral?: boolean; interactive?: boolean; onPlayWallSubmission?: (submission: Submission) => void }) {
  if (submissions.length === 0 && queueItems.length === 0) return null;
  if (neutral) {
    return (
      <aside className={compact ? 'screen-wall compact' : 'screen-wall'} aria-label="Операторская стена поздравлений">
        <p>{compact ? 'Уже показали' : 'Стена поздравлений'}</p>
        <div>
          {submissions.slice(0, 12).map((submission) => (
            <figure key={submission.id} title={submission.guest_display_name || `Гость ${submission.telegram_user_id}`}>
              {submission.thumbnail_path ? <img src={mediaUrl(submission.thumbnail_path)} alt={submission.guest_display_name || 'Поздравление'} /> : <span />}
            </figure>
          ))}
        </div>
      </aside>
    );
  }
  const items = queueItems.length > 0 ? queueItems.slice(0, 12) : submissions.slice(0, 12).map((submission) => ({ id: submission.id, submission }) as QueueItem);
  return (
    <aside className={[compact ? 'ornament-wall compact' : 'ornament-wall', interactive ? 'interactive' : ''].filter(Boolean).join(' ')} aria-label={interactive ? 'Интерактивная стена на ёлке' : 'Операторская стена поздравлений'}>
      {items.map((item, index) => (
        <Ornament key={queueItems.length > 0 ? `${item.submission_id}-${item.position}` : item.id} submission={item.submission} index={index} compact={compact} interactive={interactive} onPlayWallSubmission={onPlayWallSubmission} />
      ))}
    </aside>
  );
}

function Ornament({ submission, index, compact, interactive, onPlayWallSubmission }: { submission: Submission; index: number; compact: boolean; interactive: boolean; onPlayWallSubmission?: (submission: Submission) => void }) {
  const positions = screenThemes.new_year.positions;
  const position = positions[index % positions.length];
  const style = {
    '--ornament-x': `${position.x}%`,
    '--ornament-y': `${position.y}%`,
    '--ornament-scale': compact ? position.s * 0.68 : position.s,
    '--ornament-delay': `${(index % 6) * 0.22}s`,
    '--ornament-duration': `${5.6 + (index % 5) * 0.55}s`,
  } as React.CSSProperties;
  const content = (
    <>
      {submission.thumbnail_path && <img className="ornament-photo" src={mediaUrl(submission.thumbnail_path)} alt={submission.guest_display_name || 'Поздравление'} />}
      <img className="ornament-frame" src={screenThemes.new_year.assets.ornament} alt="" aria-hidden="true" />
    </>
  );
  if (interactive && onPlayWallSubmission) {
    return <button className="ornament interactive" style={style} title={submission.guest_display_name || `Гость ${submission.telegram_user_id}`} type="button" onClick={() => onPlayWallSubmission(submission)}>{content}</button>;
  }
  return (
    <figure className="ornament" style={style} title={submission.guest_display_name || `Гость ${submission.telegram_user_id}`}>{content}</figure>
  );
}

function CircleWall({ submissions, queueItems = [], compact = false, interactive = false, onPlayWallSubmission }: { submissions: Submission[]; queueItems?: QueueItem[]; compact?: boolean; interactive?: boolean; onPlayWallSubmission?: (submission: Submission) => void }) {
  if (submissions.length === 0 && queueItems.length === 0) return null;
  const items = queueItems.length > 0 ? queueItems.slice(0, 12) : submissions.slice(0, 12).map((submission) => ({ id: submission.id, submission }) as QueueItem);
  return (
    <aside className={[compact ? 'circle-wall compact' : 'circle-wall', interactive ? 'interactive' : ''].filter(Boolean).join(' ')} aria-label={interactive ? 'Интерактивная стена' : 'Операторская стена поздравлений'}>
      {items.map((item, index) => <CircleItem key={queueItems.length > 0 ? `${item.submission_id}-${item.position}` : item.id} submission={item.submission} index={index} compact={compact} interactive={interactive} onPlayWallSubmission={onPlayWallSubmission} />)}
    </aside>
  );
}

function CircleItem({ submission, index, compact, interactive, onPlayWallSubmission }: { submission: Submission; index: number; compact: boolean; interactive: boolean; onPlayWallSubmission?: (submission: Submission) => void }) {
  const positions = screenThemes.neutral.positions;
  const position = positions[index % positions.length];
  const style = {
    '--circle-x': `${position.x}%`,
    '--circle-y': `${position.y}%`,
    '--circle-scale': compact ? position.s * 0.62 : position.s,
    '--circle-delay': `${(index % 6) * 0.2}s`,
    '--circle-duration': `${5.4 + (index % 5) * 0.48}s`,
  } as React.CSSProperties;
  const content = (
    <>
      {submission.thumbnail_path ? <img className="circle-photo" src={mediaUrl(submission.thumbnail_path)} alt={submission.guest_display_name || 'Поздравление'} /> : <span className="circle-photo" />}
      <img className="circle-frame" src={screenThemes.neutral.assets.frame} alt="" aria-hidden="true" />
    </>
  );
  if (interactive && onPlayWallSubmission) {
    return <button className="circle-item interactive" style={style} title={submission.guest_display_name || `Гость ${submission.telegram_user_id}`} type="button" onClick={() => onPlayWallSubmission(submission)}>{content}</button>;
  }
  return <figure className="circle-item" style={style} title={submission.guest_display_name || `Гость ${submission.telegram_user_id}`}>{content}</figure>;
}

function Screen({ state, error }: { state: AppState; error: string }) {
  const event = state.active_event;
  const serverCurrent = state.playback.current;
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const currentPlaybackIdRef = useRef<string | null>(null);
  const loadedPlaybackIdRef = useRef<string | null>(null);
  const startingPlaybackIdRef = useRef<string | null>(null);
  const startedPlaybackIdRef = useRef<string | null>(null);
  const completedPlaybackIdRef = useRef<string | null>(null);
  const errorPlaybackIdRef = useRef<string | null>(null);
  const closingPlaybackIdRef = useRef<string | null>(null);
  const loadTimerRef = useRef<number | null>(null);
  const heartbeatTimerRef = useRef<number | null>(null);
  const sendHeartbeatRef = useRef<(() => void) | null>(null);
  const terminalRetryTimerRef = useRef<number | null>(null);
  const pendingTerminalEventRef = useRef<{ type: 'completed' | 'error'; playbackId: string; reason?: string; attempts: number } | null>(null);
  const terminalRequestInFlightRef = useRef(false);
  const eventIdRef = useRef<string | null>(null);
  const isActiveScreenRef = useRef(false);
  const [screenId] = useState(() => getOrCreateScreenId());
  const [screenStatus, setScreenStatus] = useState<'registering' | 'active' | 'secondary'>('registering');
  const [autoplayBlocked, setAutoplayBlocked] = useState(false);
  const [screenMessage, setScreenMessage] = useState('');
  const [playbackNotice, setPlaybackNotice] = useState('');
  const [pendingCurrent, setPendingCurrent] = useState<QueueItem | null>(null);
  const [dismissedPlaybackId, setDismissedPlaybackId] = useState<string | null>(null);
  const [qrDataUrl, setQrDataUrl] = useState('');
  const isActiveScreen = screenStatus === 'active';
  const current = serverCurrent?.playback_id === dismissedPlaybackId ? null : pendingCurrent?.playback_id === dismissedPlaybackId ? null : serverCurrent || pendingCurrent;
  const currentPlaybackId = current?.playback_id || null;
  const currentVideoUrl = current?.submission.playback_path ? mediaUrl(current.submission.playback_path) : '';
  const effectivePlaybackState = pendingCurrent ? 'running' : state.playback.state;
  const nextQueued = state.queue.find((item) => item.status === 'queued' && item.submission.playback_path);
  const heartbeatDataRef = useRef({ autoplayBlocked, screenMessage, effectivePlaybackState, current });
  heartbeatDataRef.current = { autoplayBlocked, screenMessage, effectivePlaybackState, current };
  eventIdRef.current = event?.id || null;
  isActiveScreenRef.current = isActiveScreen;

  useEffect(() => {
    let cancelled = false;
    async function register() {
      try {
        const response = await fetch('/api/screen/register', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ screen_id: screenId }),
        });
        if (!response.ok) throw new Error(await readError(response, `Не удалось зарегистрировать экран (${response.status})`));
        const result = await response.json();
        if (cancelled) return;
        setScreenStatus(result.status === 'active' ? 'active' : 'secondary');
        setScreenMessage(result.status === 'active' ? '' : 'Экран трансляции уже открыт в другой вкладке.');
      } catch (err) {
        if (!cancelled) setScreenMessage(err instanceof Error ? err.message : 'Ошибка регистрации экрана');
      }
    }
    register();
    return () => {
      cancelled = true;
    };
  }, [screenId]);

  useEffect(() => {
    if (screenStatus !== 'active') {
      sendHeartbeatRef.current = null;
      return;
    }
    const heartbeat = () => {
      const video = videoRef.current;
      const { autoplayBlocked: blocked, screenMessage: currentMessage, effectivePlaybackState: currentPlaybackState, current: currentItem } = heartbeatDataRef.current;
      const stateName = blocked ? 'autoplay_blocked' : currentPlaybackState === 'paused' ? 'paused' : currentItem ? 'playing' : 'active';
      fetch(`/api/screen/${screenId}/heartbeat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          state: stateName,
          message: currentMessage || null,
          progress_sec: video && Number.isFinite(video.currentTime) ? video.currentTime : null,
          duration_sec: video && Number.isFinite(video.duration) ? video.duration : null,
        }),
      }).catch(() => undefined);
    };
    sendHeartbeatRef.current = heartbeat;
    heartbeat();
    heartbeatTimerRef.current = window.setInterval(heartbeat, 3000);
    return () => {
      if (heartbeatTimerRef.current !== null) window.clearInterval(heartbeatTimerRef.current);
      sendHeartbeatRef.current = null;
    };
  }, [screenId, screenStatus]);

  useEffect(() => {
    if (screenStatus === 'active') sendHeartbeatRef.current?.();
  }, [autoplayBlocked, screenStatus]);

  useEffect(() => {
    if (screenStatus !== 'active') return;
    const release = () => {
      fetch(`/api/screen/${screenId}/release`, { method: 'POST', keepalive: true }).catch(() => undefined);
    };
    window.addEventListener('pagehide', release);
    return () => {
      window.removeEventListener('pagehide', release);
      release();
    };
  }, [screenId, screenStatus]);

  useEffect(() => {
    if (!event?.connection_link) {
      setQrDataUrl('');
      return;
    }
    QRCode.toDataURL(event.connection_link, { margin: 4, width: 640, errorCorrectionLevel: 'M' })
      .then(setQrDataUrl)
      .catch(() => setQrDataUrl(''));
  }, [event?.connection_link]);

  useEffect(() => {
    if (serverCurrent?.playback_id && pendingCurrent?.playback_id === serverCurrent.playback_id) {
      setPendingCurrent(null);
    }
  }, [serverCurrent?.playback_id, pendingCurrent?.playback_id]);

  useEffect(() => {
    if (dismissedPlaybackId && serverCurrent?.playback_id !== dismissedPlaybackId) setDismissedPlaybackId(null);
  }, [dismissedPlaybackId, serverCurrent?.playback_id]);

  useEffect(() => {
    if (state.playback.state === 'idle' || state.playback.state === 'stopped') {
      setPendingCurrent(null);
    }
  }, [state.playback.state]);

  useEffect(() => () => clearTerminalPlaybackEvent(), []);

  useEffect(() => {
    if (!isActiveScreen) clearTerminalPlaybackEvent();
  }, [isActiveScreen]);

  useEffect(() => {
    currentPlaybackIdRef.current = currentPlaybackId;
    setAutoplayBlocked(false);
    clearTerminalPlaybackEvent();
    if (!currentPlaybackId) {
      stopVideoElement(videoRef.current);
      startedPlaybackIdRef.current = null;
      startingPlaybackIdRef.current = null;
      completedPlaybackIdRef.current = null;
      errorPlaybackIdRef.current = null;
      closingPlaybackIdRef.current = null;
      loadedPlaybackIdRef.current = null;
      clearLoadTimer();
      setPlaybackNotice('Ожидание ролика');
      return;
    }
    completedPlaybackIdRef.current = null;
    errorPlaybackIdRef.current = null;
    closingPlaybackIdRef.current = null;
    startedPlaybackIdRef.current = null;
    startingPlaybackIdRef.current = null;
    setScreenMessage('');
    setPlaybackNotice('Получен новый ролик');
  }, [currentPlaybackId]);

  useEffect(() => {
    const video = videoRef.current;
    if (!event || !isActiveScreen) return;
    if (!video) return;
    video.volume = Math.max(0, Math.min(1, event.volume / 100));
    if (error) return;
    if (!current || !currentPlaybackId || !currentVideoUrl) {
      stopVideoElement(video);
      return;
    }
    if (effectivePlaybackState === 'paused') {
      video.pause();
      clearLoadTimer();
      setPlaybackNotice('Показ приостановлен');
      return;
    }
    if (effectivePlaybackState === 'stopped' || effectivePlaybackState === 'idle') {
      stopVideoElement(video);
      clearLoadTimer();
      setPlaybackNotice(effectivePlaybackState === 'idle' ? 'Очередь запущена, ожидаем ролик' : 'Показ остановлен');
      return;
    }
    if (effectivePlaybackState !== 'running') return;
    startCurrentVideo(currentPlaybackId, currentVideoUrl);
  }, [currentPlaybackId, currentVideoUrl, error, event?.volume, isActiveScreen, effectivePlaybackState, current?.id]);

  useEffect(() => {
    if (error) videoRef.current?.pause();
  }, [error]);

  function clearLoadTimer() {
    if (loadTimerRef.current !== null) {
      window.clearTimeout(loadTimerRef.current);
      loadTimerRef.current = null;
    }
  }

  async function postPlaybackEvent(type: 'started' | 'completed' | 'error', playbackId: string, reason?: string) {
    const eventId = eventIdRef.current;
    if (!eventId || !isActiveScreenRef.current) return false;
    try {
      const response = await fetch(`/api/events/${eventId}/playback/events`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ type, playback_id: playbackId, screen_id: screenId, reason }),
      });
      return response.ok;
    } catch {
      return false;
    }
  }

  function clearTerminalPlaybackEvent() {
    if (terminalRetryTimerRef.current !== null) {
      window.clearTimeout(terminalRetryTimerRef.current);
      terminalRetryTimerRef.current = null;
    }
    pendingTerminalEventRef.current = null;
  }

  function scheduleTerminalPlaybackRetry() {
    const pending = pendingTerminalEventRef.current;
    if (!pending || terminalRetryTimerRef.current !== null) return;
    const delay = Math.min(4000, 1000 * (2 ** Math.min(pending.attempts, 2)));
    pending.attempts += 1;
    terminalRetryTimerRef.current = window.setTimeout(() => {
      terminalRetryTimerRef.current = null;
      flushTerminalPlaybackEvent();
    }, delay);
  }

  function flushTerminalPlaybackEvent() {
    const pending = pendingTerminalEventRef.current;
    if (!pending || terminalRequestInFlightRef.current) return;
    if (currentPlaybackIdRef.current !== pending.playbackId) {
      clearTerminalPlaybackEvent();
      return;
    }
    terminalRequestInFlightRef.current = true;
    postPlaybackEvent(pending.type, pending.playbackId, pending.reason)
      .then((accepted) => {
        if (pendingTerminalEventRef.current !== pending) return;
        if (accepted) clearTerminalPlaybackEvent();
        else scheduleTerminalPlaybackRetry();
      })
      .finally(() => {
        terminalRequestInFlightRef.current = false;
        if (pendingTerminalEventRef.current && terminalRetryTimerRef.current === null) flushTerminalPlaybackEvent();
      });
  }

  function queueTerminalPlaybackEvent(type: 'completed' | 'error', playbackId: string, reason?: string) {
    const pending = pendingTerminalEventRef.current;
    if (pending?.playbackId === playbackId && pending.type === type) return;
    clearTerminalPlaybackEvent();
    pendingTerminalEventRef.current = { type, playbackId, reason, attempts: 0 };
    flushTerminalPlaybackEvent();
  }

  async function startShowFromScreen() {
    if (!isActiveScreen) return;
    setScreenMessage('');
    if (event?.playback_mode === 'interactive_wall' && !current) {
      setPlaybackNotice('Экран готов к запуску роликов из панели оператора');
      return;
    }
    setPlaybackNotice('Запрашиваем текущий ролик у сервера');
    if (event && !current) {
      const response = await fetch(`/api/events/${event.id}/playback/start`, { method: 'POST' }).catch(() => null);
      if (!response || !response.ok) {
        setPlaybackNotice('Не удалось запустить показ');
        return;
      }
      const playback = await response.json() as { current: QueueItem | null };
      const serverCurrent = playback.current;
      const serverPlaybackId = serverCurrent?.playback_id || null;
      const serverVideoUrl = serverCurrent?.submission.playback_path ? mediaUrl(serverCurrent.submission.playback_path) : '';
      if (serverPlaybackId && serverVideoUrl) {
        setPendingCurrent(serverCurrent);
        currentPlaybackIdRef.current = serverPlaybackId;
        return;
      }
    }
    if (currentPlaybackId && currentVideoUrl) {
      startCurrentVideo(currentPlaybackId, currentVideoUrl);
    } else {
      setPlaybackNotice('Ожидаем текущий ролик от сервера');
    }
  }

  async function startCurrentVideo(playbackId: string, videoUrl: string) {
    const video = videoRef.current;
    if (!video) return;
    if (!playbackId || !videoUrl || !isActiveScreen) return;
    if (currentPlaybackIdRef.current !== playbackId) return;
    if (startingPlaybackIdRef.current === playbackId) return;
    const absoluteUrl = new URL(videoUrl, window.location.href).href;
    if (video.src !== absoluteUrl || loadedPlaybackIdRef.current !== playbackId) {
      video.pause();
      video.removeAttribute('src');
      video.load();
      video.src = videoUrl;
      video.load();
      loadedPlaybackIdRef.current = playbackId;
    }
    startingPlaybackIdRef.current = playbackId;
    clearLoadTimer();
    setPlaybackNotice('Запускаем ролик');
    loadTimerRef.current = window.setTimeout(() => {
      if (currentPlaybackIdRef.current !== playbackId || effectivePlaybackState === 'paused') return;
      reportPlaybackError(playbackId, 'Тайм-аут загрузки или старта видео');
    }, Math.max(1, state.screen.load_timeout_sec) * 1000);
    try {
      await video.play();
      if (currentPlaybackIdRef.current !== playbackId) return;
      clearLoadTimer();
      setAutoplayBlocked(false);
      setScreenMessage('');
      setPlaybackNotice('Ролик воспроизводится');
      if (startedPlaybackIdRef.current !== playbackId) {
        startedPlaybackIdRef.current = playbackId;
        await postPlaybackEvent('started', playbackId);
      }
    } catch (err) {
      clearLoadTimer();
      if (currentPlaybackIdRef.current !== playbackId) return;
      if (err instanceof DOMException && err.name === 'AbortError') return;
      if (!(err instanceof DOMException) || err.name !== 'NotAllowedError') {
        reportPlaybackError(playbackId, 'Ошибка запуска видео');
        return;
      }
      setAutoplayBlocked(true);
      setPlaybackNotice('Запуск заблокирован браузером');
      setScreenMessage('Браузер заблокировал запуск со звуком. Нажмите «Запустить показ» на этом экране.');
      fetch(`/api/screen/${screenId}/heartbeat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ state: 'autoplay_blocked', message: 'Браузер заблокировал запуск со звуком' }),
      }).catch(() => undefined);
    } finally {
      if (startingPlaybackIdRef.current === playbackId) startingPlaybackIdRef.current = null;
    }
  }

  async function playSubmissionFromWall(submission: Submission) {
    if (!event || !isActiveScreen || event.playback_mode !== 'interactive_wall') return;
    setScreenMessage('');
    setPlaybackNotice('Запускаем выбранный кружочек');
    const response = await fetch(`/api/submissions/${submission.id}/play-now`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ screen_id: screenId }),
    }).catch(() => null);
    if (!response || !response.ok) {
      setPlaybackNotice('Не удалось запустить выбранный ролик');
      if (response) setScreenMessage(await readError(response, 'Команда не выполнена'));
      return;
    }
    const playback = await response.json() as { current: QueueItem | null };
    const serverCurrent = playback.current;
    const serverPlaybackId = serverCurrent?.playback_id || null;
    const serverVideoUrl = serverCurrent?.submission.playback_path ? mediaUrl(serverCurrent.submission.playback_path) : '';
    if (serverPlaybackId && serverVideoUrl) {
      setPendingCurrent(serverCurrent);
      currentPlaybackIdRef.current = serverPlaybackId;
    }
  }

  function handleEnded() {
    const playbackId = currentPlaybackIdRef.current;
    if (!playbackId || completedPlaybackIdRef.current === playbackId) return;
    if (event?.playback_mode === 'interactive_wall') {
      clearLoadTimer();
      setPlaybackNotice('Ролик завершён. Повторите видео или закройте его, чтобы вернуться к стене.');
      return;
    }
    completedPlaybackIdRef.current = playbackId;
    clearLoadTimer();
    setPlaybackNotice('Ролик завершён, ожидаем следующий');
    queueTerminalPlaybackEvent('completed', playbackId);
  }

  async function replayInteractiveVideo() {
    if (event?.playback_mode !== 'interactive_wall') return;
    const video = videoRef.current;
    if (!video || !currentPlaybackIdRef.current) return;
    clearLoadTimer();
    video.currentTime = 0;
    try {
      await video.play();
      setScreenMessage('');
      setPlaybackNotice('Ролик воспроизводится повторно');
    } catch {
      setPlaybackNotice('Запуск заблокирован браузером');
      setScreenMessage('Браузер заблокировал запуск со звуком. Нажмите «Повторить видео».');
    }
  }

  async function closeInteractiveVideo() {
    const playbackId = currentPlaybackIdRef.current;
    if (!playbackId || event?.playback_mode !== 'interactive_wall' || completedPlaybackIdRef.current === playbackId || closingPlaybackIdRef.current === playbackId) return;
    closingPlaybackIdRef.current = playbackId;
    clearLoadTimer();
    videoRef.current?.pause();
    setPlaybackNotice('Закрываем ролик...');
    const completed = await postPlaybackEvent('completed', playbackId);
    closingPlaybackIdRef.current = null;
    if (!completed) {
      setPlaybackNotice('Не удалось закрыть ролик');
      setScreenMessage('Сервер не подтвердил завершение. Проверьте соединение и повторите попытку.');
      return;
    }
    completedPlaybackIdRef.current = playbackId;
    setPendingCurrent(null);
    setDismissedPlaybackId(playbackId);
    setScreenMessage('');
    setPlaybackNotice('Возвращаемся к стене');
  }

  function handleVideoError() {
    const playbackId = currentPlaybackIdRef.current;
    if (!playbackId) return;
    reportPlaybackError(playbackId, 'Ошибка загрузки или воспроизведения видео');
  }

  function reportPlaybackError(playbackId: string, reason: string) {
    if (currentPlaybackIdRef.current !== playbackId || errorPlaybackIdRef.current === playbackId) return;
    errorPlaybackIdRef.current = playbackId;
    clearLoadTimer();
    setScreenMessage(reason);
    setPlaybackNotice('Ошибка воспроизведения');
    queueTerminalPlaybackEvent('error', playbackId, reason);
  }

  if (screenStatus === 'secondary') {
    return (
      <main className="theme-screen theme-new-year is-idle">
        <NewYearBackground />
        <div className="screen-card">
          <p className="eyebrow">Экран трансляции · занято</p>
          <h1>{event?.company_name || 'Visual Pozdr'}</h1>
          <p className="screen-text">Экран трансляции уже открыт в другой вкладке.</p>
          <p className="screen-hint">Эта вкладка не будет запускать звук и управлять показом.</p>
        </div>
      </main>
    );
  }

  return (
    <>
      <ThemeRenderer
        event={event}
        state={state}
        current={current}
        currentVideoUrl={currentVideoUrl}
        currentPlaybackId={currentPlaybackId}
        autoplayBlocked={autoplayBlocked}
        error={error}
        playbackNotice={playbackNotice}
        screenMessage={screenMessage}
        qrDataUrl={qrDataUrl}
        videoRef={videoRef}
        onStartShow={startShowFromScreen}
        onPlayWallSubmission={playSubmissionFromWall}
        onEnded={handleEnded}
        onReplayInteractiveVideo={replayInteractiveVideo}
        onCloseInteractiveVideo={closeInteractiveVideo}
        onVideoError={handleVideoError}
      />
      {nextQueued?.submission.playback_path && <video className="preload-video" src={mediaUrl(nextQueued.submission.playback_path)} muted preload="auto" />}
    </>
  );
}

function stopVideoElement(video: HTMLVideoElement | null) {
  if (!video) return;
  video.pause();
  video.removeAttribute('src');
  video.load();
}

function getOrCreateScreenId() {
  const key = 'visual-pozdr-screen-id';
  try {
    const existing = window.sessionStorage.getItem(key);
    if (existing) return existing;
    const created = crypto.randomUUID();
    window.sessionStorage.setItem(key, created);
    return created;
  } catch {
    return crypto.randomUUID();
  }
}

function formatBytes(value: number | null) {
  if (!value) return 'нет данных';
  if (value < 1024 * 1024) return `${Math.round(value / 1024)} КБ`;
  return `${(value / 1024 / 1024).toFixed(1)} МБ`;
}

function formatDate(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString('ru-RU');
}

function mediaUrl(path: string) {
  return `/api/media/${path}`;
}

createRoot(document.getElementById('root')!).render(<App />);
