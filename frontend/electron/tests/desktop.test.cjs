const assert = require('node:assert/strict');
const { test } = require('node:test');
const { EventEmitter } = require('node:events');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { executableName, pythonPath, mediaRuntimeDir } = require('../platform.cjs');

test('Windows paths stay compatible; Mac runtime selects the native architecture', () => {
  assert.equal(executableName('ffmpeg', 'win32'), 'ffmpeg.exe');
  assert.equal(executableName('ffmpeg', 'darwin'), 'ffmpeg');
  assert.equal(pythonPath('/project', 'win32'), path.join('/project', '.venv', 'Scripts', 'python.exe'));
  assert.equal(pythonPath('/project', 'darwin'), path.join('/project', '.venv', 'bin', 'python'));
  assert.equal(mediaRuntimeDir('/project', 'darwin', 'arm64'), path.join('/project', 'runtime', 'ffmpeg', 'darwin-arm64'));
  assert.equal(mediaRuntimeDir('/project', 'darwin', 'x64'), path.join('/project', 'runtime', 'ffmpeg', 'darwin-x64'));
});

function windowsHarness(platform) {
  const calls = [];
  class Window extends EventEmitter {
    constructor(options) {
      super();
      this.options = options;
      this.fullscreen = true;
      this.destroyed = false;
      this.webContents = { setWindowOpenHandler() {}, on() {} };
    }
    isDestroyed() { return this.destroyed; }
    isFullScreen() { return this.fullscreen; }
    setFullScreen(value) { calls.push(['fullscreen', value]); this.fullscreen = value; }
    setBounds(value) { calls.push(['bounds', value]); }
    focus() {}
    loadURL() {}
  }
  const displays = [
    { id: 1, workArea: { x: 0, y: 0, width: 1440, height: 900 } },
    { id: 2, workArea: { x: 1440, y: 0, width: 1920, height: 1080 } },
  ];
  const module = { exports: {} };
  const context = {
    module, __dirname: path.resolve(__dirname, '..'), process: { platform }, setTimeout, clearTimeout,
    require(name) {
      return name === 'electron'
        ? { BrowserWindow: Window, screen: { getAllDisplays: () => displays, getPrimaryDisplay: () => displays[0] }, shell: {} }
        : require(name);
    },
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', 'windows.cjs'), 'utf8'), context);
  const api = module.exports;
  const window = api.createScreenWindow({ host: '127.0.0.1', port: 8000 });
  return { api, window, calls };
}

test('Mac defaults to a secondary display and waits for Spaces before moving', async () => {
  const { api, window, calls } = windowsHarness('darwin');
  assert.equal(window.options.x, 1440);
  assert.equal(api.getScreenState().displays[0].primary, true);
  api.selectScreenDisplay('1');
  const move = api.moveScreenWindow();
  await new Promise(setImmediate);
  assert.deepEqual(calls, [['fullscreen', false]]);
  window.emit('leave-full-screen');
  await move;
  assert.equal(calls[1][0], 'bounds');
  assert.equal(calls[1][1].x, 0);
  assert.deepEqual(calls[2], ['fullscreen', true]);
});

test('closing Mac screen during a fullscreen transition does not touch a destroyed window', async () => {
  const { api, window, calls } = windowsHarness('darwin');
  const move = api.moveScreenWindow();
  await new Promise(setImmediate);
  window.destroyed = true;
  window.emit('closed');
  await move;
  assert.equal(calls.some(([name]) => name === 'bounds'), false);
});

test('Windows screen move does not wait for macOS-only fullscreen events', async () => {
  const { api, calls } = windowsHarness('win32');
  await api.moveScreenWindow();
  assert.deepEqual(calls.map(([name]) => name), ['fullscreen', 'bounds', 'fullscreen']);
});

test('native display event objects are never forwarded over renderer IPC', async () => {
  const listeners = {};
  const state = { displays: [], screenOpen: false };
  const sent = [];
  const electron = {
    app: { setName() {}, requestSingleInstanceLock: () => true, on() {}, whenReady: () => Promise.resolve(),
      commandLine: { appendSwitch() {} } },
    ipcMain: { handle() {} }, shell: {}, dialog: {},
    BrowserWindow: { getAllWindows: () => [{ webContents: { send: (_, payload) => sent.push(payload) } }] },
    screen: { on: (name, callback) => { listeners[name] = callback; } },
  };
  const module = { exports: {} };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', 'main.cjs'), 'utf8'), {
    module, console, process: { argv: [] },
    require(name) {
      if (name === 'electron') return electron;
      if (name === './config.cjs') return { getRuntimeConfig: () => ({}) };
      if (name === './backend.cjs') return { startBackend: async () => {}, stopBackend: async () => {} };
      if (name === './windows.cjs') return {
        getScreenState: () => state, createAdminWindow() {}, onScreenWindowStateChanged() {},
      };
      return require(name);
    },
  });
  await new Promise(setImmediate);
  const nativeEvent = { sender: { nativeHandle: true } };
  listeners['display-added'](nativeEvent, { id: 2 });
  listeners['display-metrics-changed'](nativeEvent, { id: 2 }, ['bounds']);
  assert.deepEqual(sent, [state, state]);
});
