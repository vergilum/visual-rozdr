// Only called with --smoke-test and a temporary profile by our build checks.
const { setTimeout: delay } = require('node:timers/promises');
const { createScreenWindow, closeScreenWindow } = require('./windows.cjs');

async function waitFor(check, description) {
  const deadline = Date.now() + 20000;
  while (Date.now() < deadline) {
    if (await check()) return;
    await delay(200);
  }
  throw new Error(`Timeout: ${description}`);
}

async function smoke(config, adminWindow) {
  const base = `http://${config.host}:${config.port}`;
  const response = await fetch(`${base}/api/events`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ company_name: 'Mac demo smoke', event_name: 'Isolated build check' }),
  });
  if (!response.ok) throw new Error('Could not create smoke event');
  const event = await response.json();
  console.log('Smoke: event created');
  await waitFor(async () => !adminWindow.webContents.isLoadingMainFrame()
    && adminWindow.webContents.executeJavaScript("Boolean(document.querySelector('h1'))"), 'admin render');
  const screenWindow = createScreenWindow(config);
  console.log('Smoke: screen window created');
  for (const [theme, selector] of [['new_year', '.theme-new-year'], ['neutral', '.theme-neutral']]) {
    console.log(`Smoke: loading ${theme}`);
    const update = await fetch(`${base}/api/events/${event.id}`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ theme }),
    });
    if (!update.ok) throw new Error(`Could not select ${theme}`);
    await waitFor(async () => !screenWindow.webContents.isLoadingMainFrame()
      && screenWindow.webContents.executeJavaScript(`Boolean(document.querySelector('${selector}'))
        && [...document.images].length >= 3
        && [...document.images].every(image => image.complete && image.naturalWidth > 0)
        && document.querySelector('img[alt="QR-код подключения к Telegram"]')?.naturalWidth === 640
        && document.querySelector('.screen-event-name')?.textContent === 'Isolated build check'
        && document.querySelector('.screen-greeting')?.textContent === 'С Новым годом!'
        && !document.querySelector('.telegram-link')`), `${theme} assets, QR and invitation text`);
  }
  await waitFor(async () => {
    const state = await (await fetch(`${base}/api/state`)).json();
    return state.screen.status === 'active';
  }, 'active screen registration');
  await closeScreenWindow();
  console.log('PASS: packaged Electron admin, screen, both themes, QR, screen registration');
}

module.exports = { smoke };
