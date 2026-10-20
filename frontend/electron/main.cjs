const { app, dialog, ipcMain, shell } = require('electron');
const fs = require('node:fs');
const path = require('node:path');
const { getRuntimeConfig } = require('./config.cjs');
const { startBackend, stopBackend } = require('./backend.cjs');
const {
  closeScreenWindow,
  createAdminWindow,
  createScreenWindow,
  getScreenState,
  getScreenWindow,
  moveScreenWindow,
  onScreenWindowStateChanged,
  selectScreenDisplay,
} = require('./windows.cjs');

let config;
let quitting = false;

app.setName('Visual Pozdr');
const smokeTest = process.argv.includes('--smoke-test');
if (smokeTest) {
  if (!app.isPackaged) throw new Error('Smoke test is only supported for a packaged application.');
  const profile = process.env.VISUAL_POZDR_SMOKE_PROFILE;
  if (!profile || !path.isAbsolute(profile) || !fs.statSync(profile).isDirectory()) {
    throw new Error('Smoke test requires an existing absolute temporary profile directory.');
  }
  app.setPath('userData', profile);
}

// The dedicated local ScreenWindow is operator-controlled and may receive
// server-driven playback commands before anyone clicks inside that window.
app.commandLine.appendSwitch('autoplay-policy', 'no-user-gesture-required');

function broadcastScreenState(state = getScreenState()) {
  for (const window of require('electron').BrowserWindow.getAllWindows()) {
    window.webContents.send('desktop:screen-state-changed', state);
  }
}

async function handleDisplayRemoved() {
  const state = getScreenState();
  try {
    if (state.warning && state.screenOpen) await moveScreenWindow();
    broadcastScreenState(getScreenState());
  } catch (error) {
    broadcastScreenState({ ...getScreenState(), warning: error.message });
  }
}

if (!app.requestSingleInstanceLock()) {
  app.quit();
}

app.on('second-instance', () => createAdminWindow(config));

app.whenReady().then(async () => {
  config = getRuntimeConfig();
  if (process.argv.includes('--dev')) config.devServerUrl = 'http://127.0.0.1:5173';

  ipcMain.handle('desktop:get-screen-state', () => getScreenState());
  ipcMain.handle('desktop:select-screen-display', (_, displayId) => {
    const state = selectScreenDisplay(displayId);
    broadcastScreenState();
    return state;
  });
  ipcMain.handle('desktop:open-screen', () => {
    createScreenWindow(config);
    const state = getScreenState();
    broadcastScreenState();
    return state;
  });
  ipcMain.handle('desktop:move-screen', async () => {
    const state = await moveScreenWindow();
    broadcastScreenState();
    return state;
  });
  ipcMain.handle('desktop:close-screen', async () => {
    const state = await closeScreenWindow();
    broadcastScreenState(state);
    return state;
  });
  ipcMain.handle('desktop:toggle-screen-fullscreen', () => {
    const window = getScreenWindow();
    if (!window) return false;
    window.setFullScreen(!window.isFullScreen());
    return window.isFullScreen();
  });
  ipcMain.handle('desktop:open-configuration', async () => {
    const error = await shell.openPath(config.configDir);
    if (error) throw new Error(`Не удалось открыть папку конфигурации: ${error}`);
  });
  ipcMain.handle('desktop:runtime', () => ({ packaged: config.packaged, host: config.host, port: config.port }));

  const { screen } = require('electron');
  onScreenWindowStateChanged(broadcastScreenState);
  screen.on('display-added', () => broadcastScreenState());
  screen.on('display-removed', handleDisplayRemoved);
  screen.on('display-metrics-changed', () => broadcastScreenState());

  try {
    await startBackend(config);
    const adminWindow = createAdminWindow(config);
    if (smokeTest) {
      await require('./smoke.cjs').smoke(config, adminWindow);
      app.quit();
    }
  } catch (error) {
    if (smokeTest) {
      console.error(error);
      process.exitCode = 1;
      await stopBackend();
      app.exit(1);
      return;
    }
    await dialog.showMessageBox({
      type: 'error',
      title: 'Visual Pozdr',
      message: 'Не удалось запустить локальный сервер.',
      detail: error instanceof Error ? error.message : String(error),
    });
    app.quit();
  }
});

app.on('window-all-closed', () => app.quit());
app.on('before-quit', (event) => {
  if (quitting) return;
  quitting = true;
  event.preventDefault();
  stopBackend().finally(() => app.quit());
});
