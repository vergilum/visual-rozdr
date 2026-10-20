const assert = require('node:assert/strict');
const { test } = require('node:test');
const path = require('node:path');
const { getConfig, validateConfiguration } = require('app-builder-lib/out/util/config/config');
const { DebugLogger } = require('builder-util');

for (const config of ['electron-builder.yml', 'electron-builder.portable.yml', 'electron-builder.mac.yml']) {
  test(`${config} includes shared resources once and only its own media runtime`, async () => {
    const root = path.resolve(__dirname, '..', '..');
    const merged = await getConfig(root, config, null);
    await validateConfiguration(merged, new DebugLogger(false));
    assert.equal(merged.extraResources.length, 4);
    const runtimes = merged.extraResources.filter(resource => resource.to === 'runtime/ffmpeg');
    assert.equal(runtimes.length, 1);
    assert.equal(runtimes[0].from, config.endsWith('.mac.yml') ? '../runtime/ffmpeg/darwin-${arch}' : '../runtime/ffmpeg');
    if (config.endsWith('.mac.yml')) assert.equal(merged.mac.identity, '-');
  });
}
