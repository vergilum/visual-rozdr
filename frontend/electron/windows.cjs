const { EventEmitter } = require('node:events');
const path = require('node:path');
const { BrowserWindow, screen, shell } = require('electron');

let adminWindow = null;
let screenWindow = null;
let selectedDisplayId = null;
let moveChain = Promise.resolve();
const screenWindowEvents = new EventEmitter();

function openSafeExternal(url) {
  const protocol = new URL(url).protocol;
  if (protocol === 'http:' || protocol === 'https:') shell.openExternal(url);
}

function serverUrl(config, route) {
  return `http://${config.host}:${config.port}${route}`;
}

function windowOptions() {
  return {
    show: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
    },
  };
}

function toDisplayInfo(display) {
  return {
    id: String(display.id),
    name: display.label || (display.primary ? 'Основной монитор' : `Монитор ${display.id}`),
    bounds: { ...display.bounds },
    workArea: { ...display.workArea },
    scaleFactor: display.scaleFactor,
    primary: display.id === screen.getPrimaryDisplay().id,
  };
}

function resolveSelectedDisplay() {
  const displays = screen.getAllDisplays();
  const selected = displays.find((display) => String(display.id) === selectedDisplayId);
  if (selected) return { display: selected, warning: null };

  const primary = screen.getPrimaryDisplay();
  const warning = selectedDisplayId ? 'Выбранный монитор отключён. Для экрана показа выбран основной монитор.' : null;
  selectedDisplayId = String(primary.id);
  return { display: primary, warning };
}

function getScreenState() {
  const { display, warning } = resolveSelectedDisplay();
  return {
    displays: screen.getAllDisplays().map(toDisplayInfo),
    selectedDisplayId: String(display.id),
    screenOpen: Boolean(screenWindow && !screenWindow.isDestroyed()),
    warning,
  };
}

async function placeScreenWindow(targetDisplay) {
  if (!screenWindow || screenWindow.isDestroyed()) return;
  const window = screenWindow;
  const wasFullscreen = window.isFullScreen();
  if (wasFullscreen && process.platform === 'darwin') {
    // Native macOS Spaces transitions finish asynchronously; moving earlier
    // can leave the fullscreen window on its old display.
    await new Promise((resolve, reject) => {
      const cleanup = () => {
        clearTimeout(timer);
        window.removeListener('leave-full-screen', left);
        window.removeListener('closed', left);
      };
      const left = () => { cleanup(); resolve(); };
      const timer = setTimeout(() => {
        cleanup();
        reject(new Error('Не удалось выйти из полноэкранного режима. Повторите перенос экрана.'));
      }, 6000);
      window.once('leave-full-screen', left);
      window.once('closed', left);
      window.setFullScreen(false);
    });
  } else if (wasFullscreen) {
    window.setFullScreen(false);
  }
  if (window.isDestroyed()) return;
  window.setBounds({
    x: targetDisplay.workArea.x,
    y: targetDisplay.workArea.y,
    width: targetDisplay.workArea.width,
    height: targetDisplay.workArea.height,
  });
  if (wasFullscreen) window.setFullScreen(true);
  window.focus();
}

function selectScreenDisplay(displayId) {
  selectedDisplayId = String(displayId);
  const { warning } = resolveSelectedDisplay();
  return { ...getScreenState(), warning };
}

function configureNavigation(window, config) {
  window.webContents.setWindowOpenHandler(({ url }) => {
    if (url === serverUrl(config, '/screen')) {
      createScreenWindow(config);
      return { action: 'deny' };
    }
    openSafeExternal(url);
    return { action: 'deny' };
  });
  window.webContents.on('will-navigate', (event, url) => {
    if (!url.startsWith(serverUrl(config, '/'))) {
      event.preventDefault();
      openSafeExternal(url);
    }
  });
}

function createAdminWindow(config) {
  if (adminWindow) {
    adminWindow.focus();
    return adminWindow;
  }
  adminWindow = new BrowserWindow({ ...windowOptions(), width: 1440, height: 980, minWidth: 1024, minHeight: 720, title: 'Visual Pozdr - Operator' });
  configureNavigation(adminWindow, config);
  adminWindow.loadURL(config.devServerUrl || serverUrl(config, '/admin'));
  adminWindow.once('ready-to-show', () => adminWindow?.show());
  adminWindow.on('closed', () => { adminWindow = null; });
  return adminWindow;
}

function createScreenWindow(config) {
  if (screenWindow) {
    screenWindow.focus();
    return screenWindow;
  }
  if (selectedDisplayId === null) {
    const displays = screen.getAllDisplays();
    const primary = screen.getPrimaryDisplay();
    selectedDisplayId = String((displays.find((display) => display.id !== primary.id) || primary).id);
  }
  const { display: targetDisplay } = resolveSelectedDisplay();
  screenWindow = new BrowserWindow({
    ...windowOptions(),
    x: targetDisplay.workArea.x,
    y: targetDisplay.workArea.y,
    width: targetDisplay.workArea.width,
    height: targetDisplay.workArea.height,
    title: 'Visual Pozdr - Screen',
    frame: false,
    autoHideMenuBar: true,
    backgroundColor: '#000000',
    fullscreen: true,
  });
  configureNavigation(screenWindow, config);
  screenWindow.loadURL(config.devServerUrl ? `${config.devServerUrl}/screen` : serverUrl(config, '/screen'));
  screenWindow.once('ready-to-show', () => {
    screenWindow?.show();
    screenWindow?.setFullScreen(true);
  });
  screenWindow.on('closed', () => {
    screenWindow = null;
    screenWindowEvents.emit('changed');
  });
  return screenWindow;
}

function moveScreenWindow() {
  moveChain = moveChain.catch(() => {}).then(async () => {
    const { display } = resolveSelectedDisplay();
    await placeScreenWindow(display);
    return getScreenState();
  });
  return moveChain;
}

function closeScreenWindow() {
  if (!screenWindow || screenWindow.isDestroyed()) return Promise.resolve(getScreenState());
  return new Promise((resolve) => {
    screenWindow.once('closed', () => resolve(getScreenState()));
    screenWindow.close();
  });
}

function getScreenWindow() {
  return screenWindow;
}

module.exports = {
  closeScreenWindow,
  createAdminWindow,
  createScreenWindow,
  getScreenState,
  getScreenWindow,
  moveScreenWindow,
  onScreenWindowStateChanged: (listener) => screenWindowEvents.on('changed', listener),
  selectScreenDisplay,
};
