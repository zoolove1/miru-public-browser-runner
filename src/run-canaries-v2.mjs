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

async function closeContext(context) {
  await Promise.race([
    context.close().catch(() => {}),
    new Promise((resolve) => setTimeout(resolve, 5_000)),
  ]);
}

function observe(page, label) {
  page.setDefaultTimeout(20_000);
  page.setDefaultNavigationTimeout(60_000);
  page.on('console', (message) => diagnostics.push({ label, type: message.type(), text: message.text().slice(0, 1000) }));
  page.on('pageerror', (error) => diagnostics.push({ label, type: 'pageerror', text: error.message.slice(0, 2000) }));
}

async function state(page) {
  return page.locator('video').first().evaluate((video) => ({
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

async function requestPlay(page) {
  await page.locator('video').first().evaluate((video) => {
    video.muted = true;
    video.volume = 0;
    video.playsInline = true;
    void video.play().catch(() => {});
  });
}

async function run(name, task) {
  try {
    results.push({ name, ...(await task()) });
  } catch (error) {
    results.push({
      name,
      ok: false,
      status: 'FAILED',
      error: error instanceof Error ? `${error.name}: ${error.message}` : String(error),
    });
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
    await page.locator('video').waitFor({ state: 'visible' });
    await page.locator('video').evaluate((video) => new Promise((resolve, reject) => {
      if (video.readyState >= 2) return resolve();
      const timer = setTimeout(() => reject(new Error('media readiness timeout')), 25_000);
      video.addEventListener('loadeddata', () => { clearTimeout(timer); resolve(); }, { once: true });
      video.addEventListener('error', () => { clearTimeout(timer); reject(new Error('media load error')); }, { once: true });
    }));
    await requestPlay(page);
    await page.waitForTimeout(2_000);
    const before = await state(page);
    await page.screenshot({ path: path.join(evidenceDir, 'direct-before.png') });
    await page.waitForTimeout(6_000);
    const after = await state(page);
    await page.screenshot({ path: path.join(evidenceDir, 'direct-after.png') });
    const advancedBy = after.currentTime - before.currentTime;
    if (advancedBy < 2) throw new Error(`media advanced only ${advancedBy.toFixed(3)}s`);
    return { ok: true, status: 'PUBLIC_HTML5_VIDEO_PLAYBACK_VERIFIED', source, before, after, advancedBy };
  } finally {
    await closeContext(context);
  }
});

await run('youtube', async () => {
  const context = await browser.newContext({ viewport: { width: 1280, height: 720 }, locale: 'en-US' });
  const page = await context.newPage();
  observe(page, 'youtube');
  const videoId = 'YE7VzlLtp-4';
  const targetUrl = `https://www.youtube-nocookie.com/embed/${videoId}?autoplay=1&mute=1&playsinline=1&controls=1`;
  try {
    await page.goto(targetUrl, { waitUntil: 'domcontentloaded', timeout: 60_000 });
    await page.screenshot({ path: path.join(evidenceDir, 'youtube-loaded.png') });
    const video = page.locator('video').first();
    await video.waitFor({ state: 'attached', timeout: 35_000 });
    const playButton = page.locator('.ytp-large-play-button, .ytp-play-button').first();
    if (await playButton.isVisible().catch(() => false)) await playButton.click().catch(() => {});
    await requestPlay(page);
    await page.waitForTimeout(3_000);
    const before = await state(page);
    await page.screenshot({ path: path.join(evidenceDir, 'youtube-before.png') });
    await page.waitForTimeout(10_000);
    const after = await state(page);
    await page.screenshot({ path: path.join(evidenceDir, 'youtube-after.png') });
    const errorText = await page.locator('.ytp-error, #error-screen').first().textContent().catch(() => null);
    const advancedBy = after.currentTime - before.currentTime;
    if (errorText?.trim()) throw new Error(`YouTube error: ${errorText.trim().slice(0, 500)}`);
    if (advancedBy < 3) throw new Error(`YouTube advanced only ${advancedBy.toFixed(3)}s`);
    return { ok: true, status: 'PUBLIC_YOUTUBE_PLAYBACK_VERIFIED', videoId, targetUrl, before, after, advancedBy };
  } finally {
    await closeContext(context);
  }
});

await browser.close().catch(() => {});
const finalResult = {
  ok: results.every((item) => item.ok),
  status: results.every((item) => item.ok) ? 'MIRU_PUBLIC_BROWSER_RUNNER_VERIFIED' : 'MIRU_PUBLIC_BROWSER_RUNNER_FAILED',
  browserExecutablePath: executablePath || null,
  completedAt: new Date().toISOString(),
  results,
  diagnostics,
};
await writeFile(path.join(evidenceDir, 'final-result.json'), `${JSON.stringify(finalResult, null, 2)}\n`, 'utf8');
process.stdout.write(`${JSON.stringify(finalResult, null, 2)}\n`);
if (!finalResult.ok) process.exitCode = 1;
