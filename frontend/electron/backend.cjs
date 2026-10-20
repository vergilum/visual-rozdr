const { spawn } = require('node:child_process');
const fs = require('node:fs');
const net = require('node:net');
const path = require('node:path');
const { pythonPath } = require('./platform.cjs');

let backendProcess = null;
let backendLog = null;
let backendExitError = null;

function writeBackendLog(message) {
  if (backendLog) backendLog.write(`${new Date().toISOString()} ${message}\n`);
}

function closeBackendLog() {
  if (!backendLog) return;
  backendLog.end();
  backendLog = null;
}

function validatePackagedRuntime(config) {
  if (!config.packaged) return;
  const requiredFiles = [
    ['Собранный frontend', path.join(config.frontendDistDir, 'index.html')],
    ['PyInstaller backend', config.backendPath],
    ['FFmpeg', config.ffmpegPath],
    ['ffprobe', config.ffprobePath],
  ];
  const missing = requiredFiles.filter(([, filePath]) => !fs.existsSync(filePath));
  if (missing.length) throw new Error(`Не найдены обязательные файлы приложения:\n${missing.map(([name, filePath]) => `${name}: ${filePath}`).join('\n')}`);
}

function backendCommand(config) {
  if (config.packaged) {
    return {
      command: config.backendPath,
      args: [],
    };
  }

  const python = process.env.VISUAL_POZDR_PYTHON || pythonPath(config.projectRoot);
  return { command: python, args: [path.join(config.projectRoot, 'run_server.py')] };
}

async function waitForHealth(config, timeoutMs = 30000) {
  const endpoint = `http://${config.host}:${config.port}/api/health`;
  const deadline = Date.now() + timeoutMs;
  let lastError = 'Backend did not respond';
  while (Date.now() < deadline) {
    if (backendExitError) throw new Error(backendExitError);
    try {
      const response = await fetch(endpoint);
      if (response.ok) return;
      lastError = `Backend returned HTTP ${response.status}`;
    } catch (error) {
      lastError = error instanceof Error ? error.message : String(error);
    }
    await new Promise((resolve) => setTimeout(resolve, 400));
  }
  throw new Error(`Could not start local server at ${endpoint}: ${lastError}`);
}

async function isPortAvailable(host, port) {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once('error', (error) => {
      if (error.code === 'EADDRINUSE') resolve(false);
      else reject(error);
    });
    server.listen({ host, port, exclusive: true }, () => {
      server.close((error) => {
        if (error) reject(error);
        else resolve(true);
      });
    });
  });
}

async function selectBackendPort(config) {
  const endpoint = `http://${config.host}:${config.port}/api/health`;
  try {
    const existing = await fetch(endpoint);
    if (existing.ok) throw new Error(`Visual Pozdr уже запущен на порту ${config.port}. Закройте работающее приложение перед повторным запуском.`);
  } catch (error) {
    if (error instanceof Error && error.message.includes('Visual Pozdr уже запущен')) throw error;
  }
  if (await isPortAvailable(config.host, config.port)) return;
  if (!config.packaged) throw new Error(`Порт ${config.port} уже используется другим приложением.`);

  const requestedPort = config.port;
  for (let port = requestedPort + 1; port <= Math.min(requestedPort + 20, 65535); port += 1) {
    if (await isPortAvailable(config.host, port)) {
      config.port = port;
      return;
    }
  }
  throw new Error(`Не найден свободный локальный порт рядом с ${requestedPort}.`);
}

async function startBackend(config) {
  if (backendProcess) return;
  await selectBackendPort(config);
  validatePackagedRuntime(config);
  const { command, args } = backendCommand(config);
  if (!fs.existsSync(command)) {
    throw new Error(`Backend runtime was not found: ${command}`);
  }

  fs.mkdirSync(config.dataDir, { recursive: true });
  fs.mkdirSync(config.logsDir, { recursive: true });
  backendLog = fs.createWriteStream(path.join(config.logsDir, 'backend.log'), { flags: 'a' });
  backendExitError = null;
  writeBackendLog(`Starting backend on ${config.host}:${config.port}`);
  const packagedToolPaths = config.packaged ? {
    FFMPEG_PATH: config.ffmpegPath,
    FFPROBE_PATH: config.ffprobePath,
  } : {};
  backendProcess = spawn(command, args, {
    cwd: config.packaged ? config.dataDir : config.projectRoot,
    env: {
      ...process.env,
      ...config.env,
      HOST: config.host,
      PORT: String(config.port),
      DATA_DIR: config.dataDir,
      FRONTEND_DIST_DIR: config.frontendDistDir,
      ...packagedToolPaths,
      PYTHONUTF8: '1',
      PYTHONIOENCODING: 'utf-8',
      VISUAL_POZDR_DESKTOP_CONTROL: '1',
    },
    stdio: ['pipe', 'pipe', 'pipe'],
    windowsHide: true,
    // A separate POSIX process group lets the fallback stop FFmpeg as well.
    detached: process.platform !== 'win32',
  });
  backendProcess.stdin.on('error', () => {
    writeBackendLog('Backend control pipe closed');
  });
  backendProcess.stdout.on('data', (chunk) => writeBackendLog(`[stdout] ${chunk.toString().trimEnd()}`));
  backendProcess.stderr.on('data', (chunk) => writeBackendLog(`[stderr] ${chunk.toString().trimEnd()}`));
  backendProcess.once('error', (error) => {
    backendExitError = `Не удалось запустить backend: ${error.message}`;
    writeBackendLog(backendExitError);
  });
  backendProcess.once('exit', (code) => {
    backendExitError = `Backend завершился с кодом ${code ?? 'unknown'}`;
    writeBackendLog(backendExitError);
    backendProcess = null;
    closeBackendLog();
  });

  try {
    await waitForHealth(config);
    writeBackendLog('Backend health check passed');
  } catch (error) {
    await stopBackend();
    throw new Error(`${error instanceof Error ? error.message : String(error)}\nЛог backend: ${path.join(config.logsDir, 'backend.log')}`);
  }
}

function waitForExit(process, timeoutMs) {
  if (process.exitCode !== null || process.signalCode !== null) return Promise.resolve();
  return new Promise((resolve) => {
    const finish = () => {
      clearTimeout(timer);
      process.removeListener('exit', finish);
      process.removeListener('error', finish);
      resolve();
    };
    const timer = setTimeout(finish, timeoutMs);
    process.once('exit', finish);
    process.once('error', finish);
  });
}

async function stopBackend() {
  const child = backendProcess;
  if (!child) {
    closeBackendLog();
    return;
  }
  writeBackendLog('Stopping backend');
  if (child.stdin.writable) child.stdin.end('shutdown\n');
  await waitForExit(child, 10000);
  if (child.exitCode === null && child.signalCode === null && child.pid) {
    writeBackendLog(`Backend did not exit in time; terminating process tree ${child.pid}`);
    if (process.platform === 'win32') {
      const killer = spawn('taskkill', ['/pid', String(child.pid), '/t', '/f'], { windowsHide: true, stdio: 'ignore' });
      await waitForExit(killer, 5000);
    } else {
      try {
        process.kill(-child.pid, 'SIGKILL');
      } catch (error) {
        if (error.code !== 'ESRCH') throw error;
      }
    }
    await waitForExit(child, 1000);
  }
  backendProcess = null;
  closeBackendLog();
}

module.exports = { startBackend, stopBackend };
