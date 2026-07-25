#!/usr/bin/env node
import { chromium } from 'playwright';
import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import process from 'node:process';

const evidenceDir = path.resolve(process.env.EVIDENCE_DIR || 'evidence');
await mkdir(evidenceDir, { recursive: true });
const results = [];
const diagnostics = [];
const executablePath = String(process.env.BROWSER_EXECUTABLE_PATH || '').trim();
const browser = await chromium.launch({
  headless: true,
  ...(executablePath ? { executablePath } : {}),
  args: ['--autoplay-policy=no-user-gesture-required', '--disable-dev-shm-usage', '--no-first-run'],
});

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function closeContext(context) {
  await Promise.race([context.close().catch(() => {}), sleep(5_000)]);
}
function observe(page, label) {
  page.setDefaultTimeout(20_000);
  page.setDefaultNavigationTimeout(60_000);
  page.on('console', (message) => diagnostics.push({ label, type: message.type(), text: message.text().slice(0, 1000) }));
  page.on('pageerror', (error) => diagnostics.push({ label, type: 'pageerror', text: error.message.slice(0, 2000) }));
}
async function videoState(scope) {
  return scope.locator('video').first().evaluate((video) => ({
    currentTime: Number(video.currentTime || 0),
    duration: Number.isFinite(video.duration) ? Number(video.duration) : null,
    paused: Boolean(video.paused),
    readyState: Number(video.readyState),
    networkState: Number(video.networkState),
    videoWidth: Number(video.videoWidth || 0),
    videoHeight: Number(video.videoHeight || 0),
    error: video.error ? { code: video.error.code, message: video.error.message || '' } : null,
  }));
}
async function requestPlay(scope) {
  await scope.locator('video').first().evaluate((video) => {
    video.muted = true;
    video.volume = 0;
    video.playsInline = true;
    void video.play().catch(() => {});
  });
}
async function samplePlayback(page, scope, prefix) {
  await scope.locator('video').first().waitFor({ state: 'attached', timeout: 35_000 });
  const playButton = scope.locator('.ytp-large-play-button, .ytp-play-button').first();
  if (await playButton.isVisible().catch(() => false)) await playButton.click().catch(() => {});
  await requestPlay(scope);
  await sleep(3_000);
  const before = await videoState(scope);
  await page.screenshot({ path: path.join(evidenceDir, `${prefix}-before.png`) });
  await sleep(10_000);
  const after = await videoState(scope);
  await page.screenshot({ path: path.join(evidenceDir, `${prefix}-after.png`) });
  return { before, after, advancedBy: after.currentTime - before.currentTime };
}
async function run(name, task) {
  try {
    results.push({ name, ...(await task()) });
  } catch (error) {
    results.push({ name, ok: false, status: 'FAILED', error: error instanceof Error ? `${error.name}: ${error.message}` : String(error) });
  }
}

await run('startup', async () => {
  const context = await browser.newContext({ viewport: { width: 1280, height: 720 } });
  const page = await context.newPage();
  observe(page, 'startup');
  try {
    await page.setContent('<!doctype html><title>MIRU_BROWSER_STARTUP_OK</title><h1>MIRU_BROWSER_STARTUP_OK</h1>');
    await page.screenshot({ path: path.join(evidenceDir, 'startup.png') });
    const title = await page.title();
    if (title !== 'MIRU_BROWSER_STARTUP_OK') throw new Error(`unexpected title: ${title}`);
    return { ok: true, status: 'BROWSER_STARTUP_VERIFIED', title, browserVersion: browser.version() };
  } finally {
    await closeContext(context);
  }
});

await run('directMedia', async () => {
  const context = await browser.newContext({ viewport: { width: 1280, height: 720 } });
  const page = await context.newPage();
  observe(page, 'direct-media');
  const source = 'https://interactive-examples.mdn.mozilla.net/media/cc0-videos/flower.mp4';
  try {
    await page.setContent(`<video controls muted playsinline style="width:960px;height:540px" src="${source}"></video>`);
    await page.locator('video').evaluate((video) => new Promise((resolve, reject) => {
      if (video.readyState >= 2) return resolve();
      const timer = setTimeout(() => reject(new Error('media readiness timeout')), 25_000);
      video.addEventListener('loadeddata', () => { clearTimeout(timer); resolve(); }, { once: true });
      video.addEventListener('error', () => { clearTimeout(timer); reject(new Error('media load error')); }, { once: true });
    }));
    await requestPlay(page);
    await sleep(2_000);
    const before = await videoState(page);
    await page.screenshot({ path: path.join(evidenceDir, 'direct-before.png') });
    await sleep(6_000);
    const after = await videoState(page);
    await page.screenshot({ path: path.join(evidenceDir, 'direct-after.png') });
    const advancedBy = after.currentTime - before.currentTime;
    if (advancedBy < 2) throw new Error(`media advanced only ${advancedBy.toFixed(3)}s`);
    return { ok: true, status: 'PUBLIC_HTML5_VIDEO_PLAYBACK_VERIFIED', source, before, after, advancedBy };
  } finally {
    await closeContext(context);
  }
});

await run('youtube', async () => {
  const attempts = [];
  const videoId = 'M7lc1UVf-VE';

  const directContext = await browser.newContext({
    viewport: { width: 1280, height: 720 },
    locale: 'en-US',
    extraHTTPHeaders: { Referer: 'https://github.com/zoolove1/miru-public-browser-runner/' },
  });
  const directPage = await directContext.newPage();
  observe(directPage, 'youtube-direct');
  try {
    const appOrigin = 'https://github.com';
    const appReferrer = 'https://github.com/zoolove1/miru-public-browser-runner/';
    const targetUrl = `https://www.youtube.com/embed/${videoId}?autoplay=1&mute=1&playsinline=1&controls=1&origin=${encodeURIComponent(appOrigin)}&widget_referrer=${encodeURIComponent(appReferrer)}`;
    await directPage.goto(targetUrl, { waitUntil: 'domcontentloaded', timeout: 60_000, referer: appReferrer });
    await directPage.screenshot({ path: path.join(evidenceDir, 'youtube-direct-loaded.png') });
    const text = (await directPage.locator('body').innerText().catch(() => '')).trim();
    if (/sign in to confirm|not a bot|error 153/i.test(text)) {
      attempts.push({ route: 'direct-embed', ok: false, status: 'SITE_CHALLENGE', message: text.slice(0, 500) });
    } else {
      const sampled = await samplePlayback(directPage, directPage, 'youtube-direct');
      if (sampled.advancedBy >= 3) {
        return { ok: true, status: 'PUBLIC_YOUTUBE_PLAYBACK_VERIFIED', route: 'direct-embed', videoId, ...sampled };
      }
      attempts.push({ route: 'direct-embed', ok: false, status: 'NO_TIME_ADVANCE', ...sampled });
    }
  } catch (error) {
    attempts.push({ route: 'direct-embed', ok: false, status: 'ERROR', message: error instanceof Error ? error.message : String(error) });
  } finally {
    await closeContext(directContext);
  }

  const demoContext = await browser.newContext({ viewport: { width: 1280, height: 900 }, locale: 'en-US' });
  const demoPage = await demoContext.newPage();
  observe(demoPage, 'youtube-official-demo');
  try {
    const demoUrl = 'https://developers.google.com/youtube/youtube_player_demo';
    await demoPage.goto(demoUrl, { waitUntil: 'domcontentloaded', timeout: 60_000 });
    await demoPage.screenshot({ path: path.join(evidenceDir, 'youtube-demo-loaded.png'), fullPage: false });
    const iframe = demoPage.locator('iframe[src*="youtube.com"], iframe[src*="youtube-nocookie.com"]').first();
    await iframe.waitFor({ state: 'attached', timeout: 35_000 });
    const handle = await iframe.elementHandle();
    const frame = await handle?.contentFrame();
    if (!frame) throw new Error('official demo YouTube frame unavailable');
    const frameText = (await frame.locator('body').innerText().catch(() => '')).trim();
    if (/sign in to confirm|not a bot|error 153/i.test(frameText)) {
      attempts.push({ route: 'official-google-demo', ok: false, status: 'SITE_CHALLENGE', message: frameText.slice(0, 500) });
    } else {
      const sampled = await samplePlayback(demoPage, frame, 'youtube-demo');
      if (sampled.advancedBy >= 3) {
        return { ok: true, status: 'PUBLIC_YOUTUBE_PLAYBACK_VERIFIED', route: 'official-google-demo', videoId, demoUrl, ...sampled };
      }
      attempts.push({ route: 'official-google-demo', ok: false, status: 'NO_TIME_ADVANCE', ...sampled });
    }
  } catch (error) {
    attempts.push({ route: 'official-google-demo', ok: false, status: 'ERROR', message: error instanceof Error ? error.message : String(error) });
  } finally {
    await closeContext(demoContext);
  }

  return { ok: false, status: 'YOUTUBE_BLOCKED_BY_SITE_CHALLENGE', attempts };
});

await browser.close().catch(() => {});
const startupOk = results.find((item) => item.name === 'startup')?.ok === true;
const mediaOk = results.find((item) => item.name === 'directMedia')?.ok === true;
const youtube = results.find((item) => item.name === 'youtube');
const coreOk = startupOk && mediaOk;
const finalResult = {
  ok: coreOk,
  status: coreOk ? 'MIRU_PUBLIC_BROWSER_RUNNER_VERIFIED' : 'MIRU_PUBLIC_BROWSER_RUNNER_FAILED',
  youtubeCapability: youtube?.ok ? 'VERIFIED' : youtube?.status || 'UNVERIFIED',
  browserExecutablePath: executablePath || null,
  completedAt: new Date().toISOString(),
  results,
  diagnostics,
};
await writeFile(path.join(evidenceDir, 'final-result.json'), `${JSON.stringify(finalResult, null, 2)}\n`, 'utf8');
process.stdout.write(`${JSON.stringify(finalResult, null, 2)}\n`);
if (!coreOk) process.exitCode = 1;
