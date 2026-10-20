const fs = require('node:fs');
const path = require('node:path');
const { app } = require('electron');
const { executableName, mediaRuntimeDir } = require('./platform.cjs');

function parseEnvFile(filePath) {
  if (!fs.existsSync(filePath)) return {};
  const values = {};
  for (const rawLine of fs.readFileSync(filePath, 'utf8').split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith('#') || !line.includes('=')) continue;
    const [key, ...parts] = line.split('=');
    values[key.trim()] = parts.join('=').trim().replace(/^['"]|['"]$/g, '');
  }
  return values;
}

function ensurePackagedEnv() {
  migrateLegacyUserData();
  const configPath = path.join(app.getPath('userData'), '.env');
  if (!fs.existsSync(configPath)) {
    const templatePath = path.join(process.resourcesPath, 'config', '.env.example');
    if (!fs.existsSync(templatePath)) throw new Error(`Configuration template was not found: ${templatePath}`);
    fs.mkdirSync(path.dirname(configPath), { recursive: true });
    fs.copyFileSync(templatePath, configPath);
  }
  return configPath;
}

function migrateLegacyUserData() {
  const userDataPath = app.getPath('userData');
  const legacyPath = path.join(app.getPath('appData'), 'visual-pozdr-frontend');
  if (userDataPath === legacyPath || fs.existsSync(userDataPath) || !fs.existsSync(legacyPath)) return;
  fs.renameSync(legacyPath, userDataPath);
}

function getRuntimeConfig() {
  const packaged = app.isPackaged;
  const projectRoot = path.resolve(__dirname, '..', '..');
  const envPath = packaged ? ensurePackagedEnv() : path.join(projectRoot, '.env');
  const fileEnv = parseEnvFile(envPath);
  const env = { ...fileEnv, ...process.env };
  const host = '127.0.0.1';
  const parsedPort = Number.parseInt(env.PORT || '8000', 10);
  const port = Number.isInteger(parsedPort) && parsedPort > 0 && parsedPort <= 65535 ? parsedPort : 8000;
  const baseDir = packaged ? app.getPath('userData') : projectRoot;
  const configuredDataDir = env.DATA_DIR || 'data';
  const dataDir = path.resolve(baseDir, configuredDataDir);
  const frontendDistDir = packaged
    ? path.join(process.resourcesPath, 'frontend', 'dist')
    : path.join(projectRoot, 'frontend', 'dist');
  const logsDir = packaged ? path.join(app.getPath('userData'), 'logs') : path.join(projectRoot, 'logs');
  const runtimeDir = packaged ? path.join(process.resourcesPath, 'runtime', 'ffmpeg') : mediaRuntimeDir(projectRoot);

  return {
    packaged,
    projectRoot,
    configDir: path.dirname(envPath),
    env,
    host,
    port,
    dataDir,
    logsDir,
    backendPath: packaged ? path.join(process.resourcesPath, 'backend', executableName('visual-pozdr-backend')) : null,
    ffmpegPath: path.join(runtimeDir, executableName('ffmpeg')),
    ffprobePath: path.join(runtimeDir, executableName('ffprobe')),
    frontendDistDir,
  };
}

module.exports = { getRuntimeConfig };
