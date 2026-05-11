/**
 * Playwright-based X auto poster.
 * Reads a JSON payload, operates a dedicated persistent Chrome profile,
 * writes screenshots, and refuses to type into any textbox that was not newly added.
 */

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { execFile as execFileCallback } from 'node:child_process';
import { promisify } from 'node:util';

const execFile = promisify(execFileCallback);
const DEFAULT_CDP_PORT = Number(process.env.X_PLAYWRIGHT_CDP_PORT || 19322);

function argValue(name) {
  const index = process.argv.indexOf(name);
  return index >= 0 ? process.argv[index + 1] : '';
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function safeName(value) {
  return String(value || 'x')
    .replace(/[^a-zA-Z0-9_.-]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 80) || 'x';
}

function ensureDir(dir) {
  fs.mkdirSync(dir, { recursive: true });
}

async function openNormalChromeForLogin(profileDir) {
  ensureDir(profileDir);
  const debugPort = Number(process.env.X_PLAYWRIGHT_CDP_PORT || DEFAULT_CDP_PORT);
  const chromeArgs = [
    `--user-data-dir=${profileDir}`,
    `--remote-debugging-port=${debugPort}`,
    '--remote-debugging-address=127.0.0.1',
    `--remote-allow-origins=http://127.0.0.1:${debugPort}`,
    '--no-first-run',
    '--no-default-browser-check',
    'https://x.com/home',
  ];
  if (process.platform === 'darwin') {
    await execFile('open', ['-na', 'Google Chrome', '--args', ...chromeArgs], { timeout: 10000 });
    return;
  }
  const chrome = process.env.CHROME_PATH || 'google-chrome';
  await execFile(chrome, chromeArgs, { timeout: 10000 });
}

async function cdpEndpoint() {
  const port = Number(process.env.X_PLAYWRIGHT_CDP_PORT || DEFAULT_CDP_PORT);
  const url = `http://127.0.0.1:${port}/json/version`;
  try {
    const response = await fetch(url, { signal: AbortSignal.timeout(700) });
    if (!response.ok) return '';
    const data = await response.json();
    return data.webSocketDebuggerUrl ? `http://127.0.0.1:${port}` : '';
  } catch (_) {
    return '';
  }
}

function cleanSecret(value) {
  return String(value || '').replace(/\r?\n$/, '').trim();
}

function envValue(name) {
  return cleanSecret(process.env[name] || '');
}

function envFlag(name) {
  return /^(1|true|yes|on)$/i.test(envValue(name));
}

async function readCommandOutput(command, args, label) {
  try {
    const { stdout } = await execFile(command, args, {
      timeout: 12000,
      maxBuffer: 1024 * 1024,
      env: process.env,
    });
    return cleanSecret(stdout);
  } catch (error) {
    throw new Error(`${label} could not be read: ${error?.message || String(error)}`);
  }
}

async function readOnePasswordRef(ref, label) {
  if (!ref) return '';
  return readCommandOutput('op', ['read', ref], label);
}

async function readKeychainPassword(service, account, label) {
  if (!service || !account) return '';
  return readCommandOutput('/usr/bin/security', [
    'find-generic-password',
    '-s',
    service,
    '-a',
    account,
    '-w',
  ], label);
}

async function tryReadKeychainPassword(service, account) {
  if (!service || !account) return '';
  try {
    return await readKeychainPassword(service, account, 'macOS Keychain credential');
  } catch (_) {
    return '';
  }
}

function uniquePairs(pairs) {
  const seen = new Set();
  return pairs.filter(([service, account]) => {
    if (!service || !account) return false;
    const key = `${service}\n${account}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

async function resolveLoginCredentials() {
  let identifier =
    envValue('X_LOGIN_IDENTIFIER') ||
    await readOnePasswordRef(envValue('X_LOGIN_1PASSWORD_IDENTIFIER_REF'), 'X login identifier from 1Password');

  const identifierKeychainPairs = uniquePairs([
    [
      envValue('X_LOGIN_IDENTIFIER_KEYCHAIN_SERVICE'),
      envValue('X_LOGIN_IDENTIFIER_KEYCHAIN_ACCOUNT'),
    ],
    ['x-post-writer-login-identifier', 'identifier'],
  ]);
  for (const [service, account] of identifierKeychainPairs) {
    if (identifier) break;
    identifier = await tryReadKeychainPassword(service, account);
  }

  let password =
    envValue('X_LOGIN_PASSWORD') ||
    await readOnePasswordRef(envValue('X_LOGIN_1PASSWORD_PASSWORD_REF'), 'X login password from 1Password');

  const passwordKeychainPairs = uniquePairs([
    [
      envValue('X_LOGIN_PASSWORD_KEYCHAIN_SERVICE'),
      envValue('X_LOGIN_PASSWORD_KEYCHAIN_ACCOUNT'),
    ],
    [
      envValue('X_LOGIN_KEYCHAIN_SERVICE'),
      envValue('X_LOGIN_KEYCHAIN_ACCOUNT') || identifier,
    ],
    ['x-post-writer-login-password', 'password'],
    [identifier ? 'x-post-writer-login' : '', identifier],
  ]);
  for (const [service, account] of passwordKeychainPairs) {
    if (password) break;
    password = await tryReadKeychainPassword(service, account);
  }

  if (!identifier || !password) return null;
  return {
    identifier,
    password,
    challengeIdentifier: envValue('X_LOGIN_CHALLENGE_IDENTIFIER') || envValue('X_LOGIN_USERNAME') || identifier,
  };
}

function normalizeText(value) {
  return String(value || '').replace(/\s+/g, ' ').trim();
}

function containsExpected(actual, expected) {
  const actualText = normalizeText(actual);
  const expectedText = normalizeText(expected);
  if (!expectedText) return true;
  if (actualText === expectedText || actualText.includes(expectedText)) return true;
  const chunks = expectedText.split(/\s+/).filter(Boolean);
  let offset = 0;
  return chunks.every(chunk => {
    const index = actualText.indexOf(chunk, offset);
    if (index < 0) return false;
    offset = index + chunk.length;
    return true;
  });
}

async function loadPlaywright() {
  try {
    return await import('playwright');
  } catch (error) {
    throw new Error(
      'Playwright is not installed. Run npm install in x-post-writer/scripts, then retry.'
    );
  }
}

async function visibleCount(locator) {
  const count = await locator.count();
  let visible = 0;
  for (let i = 0; i < count; i += 1) {
    if (await locator.nth(i).isVisible().catch(() => false)) visible += 1;
  }
  return visible;
}

async function composeRoot(page) {
  const dialogs = page.locator('div[aria-modal="true"][role="dialog"], [role="dialog"]');
  const count = await dialogs.count();
  for (let i = count - 1; i >= 0; i -= 1) {
    const dialog = dialogs.nth(i);
    if (!(await dialog.isVisible().catch(() => false))) continue;
    const boxes = dialog.locator('div[data-testid^="tweetTextarea_"][role="textbox"][contenteditable="true"]');
    if ((await visibleCount(boxes)) > 0) return dialog;
  }
  return page.locator('body');
}

function textboxLocator(root) {
  return root.locator('div[data-testid^="tweetTextarea_"][role="textbox"][contenteditable="true"], div[aria-label="Post text"][role="textbox"][contenteditable="true"]');
}

async function waitForComposeTextbox(page, timeoutMs = 25000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    await ensureLoggedIn(page);
    const root = await composeRoot(page);
    const box = await textboxForPart(root, 1);
    if (box && await box.isVisible().catch(() => false)) return { root, box };
    await sleep(350);
  }
  return { root: await composeRoot(page), box: null };
}

async function visibleTextboxes(root) {
  const boxes = textboxLocator(root);
  const count = await boxes.count();
  const result = [];
  for (let i = 0; i < count; i += 1) {
    const box = boxes.nth(i);
    if (await box.isVisible().catch(() => false)) result.push(box);
  }
  return result;
}

async function textboxCount(root) {
  return (await visibleTextboxes(root)).length;
}

async function textboxForPart(root, partIndex) {
  const exact = root.locator(`div[data-testid="tweetTextarea_${partIndex - 1}"][role="textbox"][contenteditable="true"]`);
  if ((await exact.count()) > 0 && await exact.first().isVisible().catch(() => false)) return exact.first();
  const boxes = await visibleTextboxes(root);
  return boxes[partIndex - 1] || null;
}

async function fillContentEditable(box, text) {
  await box.scrollIntoViewIfNeeded();
  await box.click();
  await box.evaluate((el, value) => {
    el.focus();
    const selection = window.getSelection();
    const range = document.createRange();
    range.selectNodeContents(el);
    selection.removeAllRanges();
    selection.addRange(range);
    document.execCommand('delete', false, null);
    el.textContent = '';
    el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'deleteContentBackward', data: null }));
    document.execCommand('insertText', false, value);
    el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: value }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  }, text || '');
  await sleep(350);
}

async function assertTextboxText(box, expected, label) {
  const actual = await box.innerText().catch(() => '');
  if (!containsExpected(actual, expected)) {
    throw new Error(`${label} text verification failed`);
  }
}

async function screenshot(page, payload, label) {
  ensureDir(payload.screenshot_dir);
  const file = path.join(payload.screenshot_dir, `${String(Date.now())}-${safeName(label)}.png`);
  const buffer = await page.screenshot({ path: file, fullPage: false });
  if (buffer.length < 10000) {
    throw new Error(`Screenshot looks blank: ${file}`);
  }
  return file;
}

async function findAddAnother(root) {
  const labels = root.locator('button, [role="button"]').filter({
    hasText: /^(Add another post|別のポストを追加|投稿を追加|さらに投稿を追加)$/,
  });
  const count = await labels.count();
  for (let i = count - 1; i >= 0; i -= 1) {
    const candidate = labels.nth(i);
    if (await candidate.isVisible().catch(() => false)) return candidate;
  }
  return null;
}

async function findPlusButton(root) {
  const selector = [
    '[data-testid="addButton"]',
    'button[aria-label="Add post"]',
    '[role="button"][aria-label="Add post"]',
    'button[aria-label="投稿を追加"]',
    '[role="button"][aria-label="投稿を追加"]',
  ].join(', ');
  const buttons = root.locator(selector);
  const count = await buttons.count();
  for (let i = count - 1; i >= 0; i -= 1) {
    const button = buttons.nth(i);
    if (await button.isVisible().catch(() => false) && await button.isEnabled().catch(() => false)) return button;
  }
  return null;
}

async function findPollButton(root) {
  const selector = [
    '[data-testid="createPollButton"]',
    'button[aria-label="Add poll"]',
    '[role="button"][aria-label="Add poll"]',
    'button[aria-label="投票を追加"]',
    '[role="button"][aria-label="投票を追加"]',
  ].join(', ');
  const buttons = root.locator(selector);
  const count = await buttons.count();
  for (let i = count - 1; i >= 0; i -= 1) {
    const button = buttons.nth(i);
    if (await button.isVisible().catch(() => false) && await button.isEnabled().catch(() => false)) return button;
  }
  return null;
}

async function removePollButtons(root) {
  let removed = 0;
  for (let i = 0; i < 8; i += 1) {
    const button = root.locator('button, [role="button"], div, span').filter({ hasText: /^(Remove poll|投票を削除)$/ }).last();
    if (!(await button.isVisible().catch(() => false))) break;
    await button.click();
    removed += 1;
    await sleep(400);
  }
  const remaining = await root.locator('button, [role="button"], div, span').filter({ hasText: /^(Remove poll|投票を削除)$/ }).count();
  if (remaining) throw new Error(`Remove poll remains: ${remaining}`);
  return removed;
}

async function waitForNewTextbox(root, beforeCount, timeoutMs = 5000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const boxes = await visibleTextboxes(root);
    if (boxes.length > beforeCount) return boxes[boxes.length - 1];
    await sleep(200);
  }
  return null;
}

async function clickAddTarget(root) {
  const addAnother = await findAddAnother(root);
  if (addAnother) {
    await addAnother.scrollIntoViewIfNeeded();
    await addAnother.click();
    return 'add-another-post';
  }
  const plus = await findPlusButton(root);
  if (plus) {
    await plus.scrollIntoViewIfNeeded();
    try {
      await plus.click({ timeout: 5000 });
    } catch (_) {
      // マスクが邪魔している場合は JS クリックで突破
      await plus.evaluate(el => el.click());
    }
    return 'plus-button';
  }
  return '';
}

async function revealPlusViaPoll(root) {
  const poll = await findPollButton(root);
  if (!poll) return false;
  await poll.scrollIntoViewIfNeeded();
  await poll.click();
  await root.locator('text=/^(Remove poll|投票を削除)$/').first().waitFor({ state: 'visible', timeout: 4000 });
  await sleep(300);
  return true;
}

async function addThreadPart(page, payload, partIndex, text) {
  const root = await composeRoot(page);
  const before = await textboxCount(root);
  if (before < partIndex - 1) {
    throw new Error(`Part ${partIndex}: previous textbox is missing`);
  }

  const previous = await textboxForPart(root, partIndex - 1);
  if (previous) {
    await previous.scrollIntoViewIfNeeded();
    await previous.click();
  }

  // マスク（backdrop）が消えるまで待つ
  const mask = page.locator('[data-testid="mask"]');
  const maskDeadline = Date.now() + 3000;
  while (Date.now() < maskDeadline) {
    if (!(await mask.isVisible().catch(() => false))) break;
    await sleep(200);
  }

  let clickedKind = await clickAddTarget(root);
  if (!clickedKind) {
    const revealed = await revealPlusViaPoll(root);
    if (revealed) clickedKind = await clickAddTarget(root);
  }
  if (!clickedKind) throw new Error(`Part ${partIndex}: add target is missing`);

  const newBox = await waitForNewTextbox(root, before, 5000);
  if (!newBox) throw new Error(`Part ${partIndex}: new textbox did not appear`);

  const after = await textboxCount(root);
  if (after <= before) throw new Error(`Part ${partIndex}: textbox count did not increase`);

  const exact = await textboxForPart(root, partIndex);
  const target = exact || newBox;
  if (!target) throw new Error(`Part ${partIndex}: target textbox is missing`);

  const targetTestId = await target.getAttribute('data-testid').catch(() => '');
  if (targetTestId && targetTestId !== `tweetTextarea_${partIndex - 1}` && after <= before) {
    throw new Error(`Part ${partIndex}: target textbox is not new`);
  }

  await fillContentEditable(target, text);
  await assertTextboxText(target, text, `Part ${partIndex}`);
  await screenshot(page, payload, `part-${partIndex}-after-input`);
}

async function copyImageToClipboard(imagePath) {
  if (!imagePath) return false;
  if (!fs.existsSync(imagePath)) throw new Error(`Image file not found: ${imagePath}`);
  const ext = path.extname(imagePath).toLowerCase();
  const imageClass = ext === '.jpg' || ext === '.jpeg' ? 'JPEG picture' : '«class PNGf»';
  const script = `set the clipboard to (read (POSIX file ${JSON.stringify(imagePath)}) as ${imageClass})`;
  await execFile('osascript', ['-e', script], { timeout: 15000 });
  return true;
}

async function waitForImageAttachment(root) {
  const deadline = Date.now() + 12000;
  while (Date.now() < deadline) {
    const attachments = root.locator('[data-testid="attachments"]');
    if ((await attachments.count()) > 0 && await attachments.first().isVisible().catch(() => false)) {
      const images = attachments.locator('img[src^="blob:https://x.com/"]');
      if ((await images.count()) > 0) return true;
    }
    await sleep(400);
  }
  return false;
}

async function attachImage(page, payload) {
  if (!payload.image_path) return;
  const root = await composeRoot(page);
  const mainBox = await textboxForPart(root, 1);
  if (!mainBox) throw new Error('Main textbox is missing before image attachment');

  await copyImageToClipboard(payload.image_path);
  await mainBox.scrollIntoViewIfNeeded();
  await mainBox.click();
  await page.keyboard.press('Meta+V');

  if (!(await waitForImageAttachment(root))) {
    const fileInput = root.locator('input[type="file"]').first();
    if ((await fileInput.count()) === 0) throw new Error('Image paste failed and file input is missing');
    await fileInput.setInputFiles(payload.image_path);
    if (!(await waitForImageAttachment(root))) throw new Error('Image attachment preview was not detected');
  }
  await screenshot(page, payload, 'image-attached');
}

async function verifyBeforePost(page, payload) {
  const root = await composeRoot(page);
  for (let i = 0; i < payload.parts.length; i += 1) {
    const box = await textboxForPart(root, i + 1);
    if (!box) throw new Error(`Part ${i + 1}: textbox missing before post`);
    await assertTextboxText(box, payload.parts[i], `Part ${i + 1}`);
  }
  if (payload.image_path && !(await waitForImageAttachment(root))) {
    throw new Error('Image attachment missing before post');
  }
  await removePollButtons(root);
  const screenshotPath = await screenshot(page, payload, 'before-post');

  // 投稿前確認: スクショをUIに送り stdin から confirm/cancel を待つ
  process.stdout.write(JSON.stringify({ status: 'awaiting_confirm', screenshot: screenshotPath }) + '\n');
  const answer = await new Promise((resolve) => {
    let buf = '';
    const onData = (chunk) => {
      buf += chunk;
      const line = buf.split('\n')[0];
      if (line !== undefined && line.trim()) {
        process.stdin.off('data', onData);
        resolve(line.trim());
      }
    };
    process.stdin.on('data', onData);
    process.stdin.resume();
  });
  if (answer !== 'confirm') {
    throw new Error('USER_CANCELLED');
  }
}

async function clickPost(page) {
  const root = await composeRoot(page);
  const buttons = root.locator('[data-testid="tweetButton"], button').filter({ hasText: /^(Post|Post all|ポスト|すべてポスト|投稿|すべて投稿)$/ });
  const count = await buttons.count();
  for (let i = count - 1; i >= 0; i -= 1) {
    const button = buttons.nth(i);
    if (await button.isVisible().catch(() => false) && await button.isEnabled().catch(() => false)) {
      await button.click();
      return;
    }
  }
  throw new Error('Post button is missing or disabled');
}

async function isLoginPage(page) {
  const loginInputs = page.locator('input[name="text"], a[href="/login"]');
  const loginText = page.getByText(/^(Log in|Sign in|ログイン)$/);
  const hasLoginInput = (await loginInputs.count()) > 0 && await loginInputs.first().isVisible().catch(() => false);
  const hasLoginText = (await loginText.count()) > 0 && await loginText.first().isVisible().catch(() => false);
  return hasLoginInput || hasLoginText || /\/i\/flow\/login|\/login/.test(page.url());
}

async function clickFirstVisibleEnabled(locator, label, timeoutMs = 8000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const count = await locator.count();
    for (let i = 0; i < count; i += 1) {
      const item = locator.nth(i);
      if (await item.isVisible().catch(() => false) && await item.isEnabled().catch(() => false)) {
        await item.click();
        return;
      }
    }
    await sleep(200);
  }
  throw new Error(`${label} is missing or disabled`);
}

async function waitForPasswordOrChallenge(page) {
  const deadline = Date.now() + 14000;
  while (Date.now() < deadline) {
    const password = page.locator('input[name="password"]').first();
    if (await password.isVisible().catch(() => false)) return 'password';

    const challenge = page.locator('input[data-testid="ocfEnterTextTextInput"], input[name="text"]').first();
    const challengeVisible = await challenge.isVisible().catch(() => false);
    const passwordCount = await page.locator('input[name="password"]').count();
    if (challengeVisible && passwordCount === 0) return 'challenge';

    await sleep(250);
  }
  return '';
}

async function autoLoginToX(page, payload) {
  const credentials = await resolveLoginCredentials();
  if (!credentials) {
    throw new Error('Dedicated Chrome profile is not logged in to X, and no login credential source is configured. Set X_LOGIN_IDENTIFIER with Keychain password settings, or set X_LOGIN_1PASSWORD_IDENTIFIER_REF and X_LOGIN_1PASSWORD_PASSWORD_REF.');
  }

  await page.goto('https://x.com/i/flow/login', { waitUntil: 'domcontentloaded' });
  const identifierInput = page.locator('input[name="text"]').first();
  await identifierInput.waitFor({ state: 'visible', timeout: 12000 });
  await identifierInput.fill(credentials.identifier);
  await screenshot(page, payload, 'x-login-after-identifier-fill');
  await clickFirstVisibleEnabled(
    page.locator('button, [role="button"]').filter({ hasText: /^(Next|次へ)$/ }),
    'X login Next button',
  );

  await sleep(700);
  await screenshot(page, payload, 'x-login-after-next-click');
  let state = await waitForPasswordOrChallenge(page);
  await screenshot(page, payload, `x-login-state-${state || 'unknown'}`);
  if (state === 'challenge') {
    const challengeInput = page.locator('input[data-testid="ocfEnterTextTextInput"], input[name="text"]').first();
    await challengeInput.fill(credentials.challengeIdentifier);
    await clickFirstVisibleEnabled(
      page.locator('button, [role="button"]').filter({ hasText: /^(Next|次へ)$/ }),
      'X login challenge Next button',
    );
    await sleep(700);
    await screenshot(page, payload, 'x-login-after-challenge');
    state = await waitForPasswordOrChallenge(page);
    await screenshot(page, payload, `x-login-state2-${state || 'unknown'}`);
  }
  if (state !== 'password') {
    await screenshot(page, payload, 'x-login-password-missing');
    throw new Error('X login did not reach the password screen. Manual verification or 2FA may be required.');
  }

  const passwordInput = page.locator('input[name="password"]').first();
  await passwordInput.fill(credentials.password);
  await clickFirstVisibleEnabled(
    page.locator('[data-testid="LoginForm_Login_Button"], button, [role="button"]').filter({ hasText: /^(Log in|ログイン)$/ }),
    'X login submit button',
  );

  const deadline = Date.now() + 20000;
  while (Date.now() < deadline) {
    if (!(await isLoginPage(page))) break;
    await sleep(500);
  }
  if (await isLoginPage(page)) {
    await screenshot(page, payload, 'x-login-still-on-login-page');
    throw new Error('X login did not complete. Manual verification, 2FA, or an account challenge may be required.');
  }
}

async function ensureLoggedIn(page) {
  if (await isLoginPage(page)) {
    throw new Error('Dedicated Chrome profile is not logged in to X. Log in once with the Playwright profile, configure Keychain/1Password credentials, then retry.');
  }
}

async function main() {
  if (process.argv.includes('--login')) {
    const profileDir = process.env.X_PLAYWRIGHT_PROFILE_DIR || path.join(os.homedir(), 'Desktop', 'X運用', 'runtime', 'playwright-x-profile');
    await openNormalChromeForLogin(profileDir);
    console.log(JSON.stringify({
      ok: true,
      mode: 'login',
      profile_dir: profileDir,
      cdp_port: DEFAULT_CDP_PORT,
      message: 'A normal Chrome window opened with the dedicated profile. Log in to X there. You can leave this Chrome open for CDP auto-posting, or quit it before persistent-profile auto-posting.',
    }));
    return;
  }

  const payloadPath = argValue('--payload');
  if (!payloadPath) throw new Error('--payload is required');
  const payload = JSON.parse(fs.readFileSync(payloadPath, 'utf8'));
  payload.profile_dir = payload.profile_dir || path.join(os.homedir(), 'Desktop', 'X運用', 'runtime', 'playwright-x-profile');
  payload.screenshot_dir = payload.screenshot_dir || path.join(os.homedir(), '.openclaw', 'media', 'browser');
  payload.parts = Array.isArray(payload.parts) ? payload.parts.map(part => String(part || '')) : [];
  if (!payload.parts.length || !payload.parts[0].trim()) throw new Error('payload.parts[0] is required');

  const { chromium } = await loadPlaywright();
  ensureDir(payload.profile_dir);
  ensureDir(payload.screenshot_dir);

  let context;
  let browser = null;
  let ownsContext = true;
  const endpoint = await cdpEndpoint();
  if (endpoint) {
    try {
      browser = await chromium.connectOverCDP(endpoint);
      context = browser.contexts()[0] || await browser.newContext();
      ownsContext = false;
    } catch (cdpErr) {
      if (/setDownloadBehavior|Browser context management/i.test(String(cdpErr?.message || ''))) {
        throw new Error(
          'Chrome が起動中ですが CDP 接続に必要なフラグが不足しています。\n' +
          'いったん Chrome を Command+Q で完全に終了してから、再度 --login を実行してください。\n' +
          '（新しい --login では --remote-allow-origins フラグが追加されます）'
        );
      }
      throw cdpErr;
    }
  } else {
    try {
      context = await chromium.launchPersistentContext(payload.profile_dir, {
        channel: 'chrome',
        headless: false,
        viewport: { width: 1365, height: 900 },
        acceptDownloads: true,
        args: ['--disable-blink-features=AutomationControlled'],
        ignoreDefaultArgs: ['--enable-automation'],
      });
      await context.addInitScript(() => {
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
      });
    } catch (error) {
      if (/ProcessSingleton|profile directory.*in use|already in use/i.test(String(error?.message || error))) {
        throw new Error('The X Playwright Chrome profile is still open. Quit the dedicated Chrome app completely with Command+Q, or rerun --login and leave that remote-debugging Chrome open before auto-posting.');
      }
      throw error;
    }
  }
  const page = await context.newPage();
  page.setDefaultTimeout(12000);

  try {
    await page.goto('https://x.com/compose/post', { waitUntil: 'domcontentloaded' });
    if (await isLoginPage(page)) {
      if (!envFlag('X_AUTO_LOGIN_ENABLED')) {
        await screenshot(page, payload, 'x-login-required-auto-login-disabled');
        throw new Error('Dedicated Chrome profile is not logged in to X. Automatic password login is disabled by default to avoid repeated new-device login alerts. Open the dedicated Chrome profile with --login, log in once manually, then retry. To force automatic login, set X_AUTO_LOGIN_ENABLED=1.');
      }
      await autoLoginToX(page, payload);
      await page.goto('https://x.com/compose/post', { waitUntil: 'domcontentloaded' });
    }
    const compose = await waitForComposeTextbox(page);
    let root = compose.root;
    let mainBox = compose.box;
    if (!mainBox) {
      await screenshot(page, payload, 'main-textbox-missing');
      throw new Error('Main textbox is missing after opening https://x.com/compose/post');
    }
    const mainText = await mainBox.innerText().catch(() => '');
    if (!containsExpected(mainText, payload.parts[0])) {
      await fillContentEditable(mainBox, payload.parts[0]);
    }
    await assertTextboxText(mainBox, payload.parts[0], 'Part 1');
    await screenshot(page, payload, 'part-1-after-input');

    await attachImage(page, payload);

    for (let i = 1; i < payload.parts.length; i += 1) {
      await addThreadPart(page, payload, i + 1, payload.parts[i]);
    }

    await verifyBeforePost(page, payload);
    if (!payload.dry_run) {
      await clickPost(page);
      await sleep(2500);
    }

    console.log(JSON.stringify({ ok: true, dry_run: !!payload.dry_run }));
  } finally {
    await page.close().catch(() => {});
    if (ownsContext) {
      await context.close();
    }
  }
}

main().catch(error => {
  console.error(JSON.stringify({
    ok: false,
    error: error?.stack || error?.message || String(error),
  }));
  process.exit(1);
});
