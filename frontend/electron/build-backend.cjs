const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { pythonPath } = require('./platform.cjs');

const root = path.resolve(__dirname, '..', '..');
const result = spawnSync(process.env.VISUAL_POZDR_PYTHON || pythonPath(root),
  [path.join(root, 'scripts', 'build_desktop_backend.py')], { cwd: root, stdio: 'inherit' });
if (result.error) console.error(result.error.message);
process.exit(result.status ?? 1);
