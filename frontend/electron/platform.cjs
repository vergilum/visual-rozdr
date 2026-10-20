const path = require('node:path');

function executableName(name, platform = process.platform) {
  return platform === 'win32' ? `${name}.exe` : name;
}

function pythonPath(root, platform = process.platform) {
  return platform === 'win32'
    ? path.join(root, '.venv', 'Scripts', 'python.exe')
    : path.join(root, '.venv', 'bin', 'python');
}

function mediaRuntimeDir(root, platform = process.platform, arch = process.arch) {
  const base = path.join(root, 'runtime', 'ffmpeg');
  return platform === 'darwin' ? path.join(base, `darwin-${arch}`) : base;
}

module.exports = { executableName, pythonPath, mediaRuntimeDir };
