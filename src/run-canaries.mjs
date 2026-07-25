#!/usr/bin/env node
import { chromium } from 'playwright';
import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import process from 'node:process';

const evidenceDir = path.resolve(process.env.EVIDENCE_DIR || 'evidence');
await mkdir(evidenceDir, { recursive: true });

const consoleEvents = [];
const pageErrors = [];
const results = [];

async function writeJson(name, value) {
  await writeFile(path.join(evidenceDir, name), `${JSON.stringify(value, null, 2)}\n`, 'utf8');
}

function launchOptions() {
  const executablePath = String(process.env.BROWSER_EXECUTABLE_PATH || '').trim();
  return {
    headless: true,
    ...(executablePath ? { executablePath } : {}),
    args: [
      '--autoplay-policy=no-user-gesture-required',
      '--disable-dev-shm-usage',
      '--disable-background-networking',
      '--no-default-browser-check',
      '--no-first-run',
    ],
  };
}

function attachDiagnostics(page, label) {
  page.on('console', (message) => {
    consoleEvents.push({ label, type: message.type(), text: message.text().slice(0, 1000) });
  });
  page.on('pageerror', (error) => {
    pageErrors.push({ label, message: error.message.slice(0, 2000) });
  });
}

async function videoState(page) {
  return page.locator('video').first().evaluate((video) => ({
    currentTime: Number(video.currentTime || 0),
    duration: Number.isFinite(video.duration) ? Number(video.duration) : null,
    paused: Boolean(video.paused),
    ended: Boolean(video.ended),
    muted: Boolean(video.muted),
    readyState: Number(video.readyState),
    networkState: Number(video.networkState),
    videoWidth: Number(video.videoWidth || 0),
    videoHeight: Number(video.videoHeight || 0),
    error: video.error ? { code: video.error.code, message: video.error.message || '' } : null,
  }));
}

async function startVideo(page) {
  await page.locator('video').first().evaluate(async (video) => {
    video.muted = true;
    video.volume = 0;
    video.playsInline = true;
    try {
      await video.play();
    } catch {
      // A visible play control is attempted by the caller when needed.
    }
  });
}

async function runStartupSmoke(browser) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 720 } });
  const page = await context.newPage();
  attachDiagnostics(page, 'startup');
  try {
    await page.setContent('<!doctype html><html><head><title>MIRU_BROWSER_STARTUP_OK</title></head><body><h1>MIRU_BROWSER_STARTUP_OK</h1></body></html>');
    const title = await page.title();
    const heading = (await page.locator('h1').textContent())?.trim();
    await page.screenshot({ path: path.join(evidenceDir, 'startup.png') });
    if (title !== 'MIRU_BROWSER_STARTUP_OK' || heading !== 'MIRU_BROWSER_STARTUP_OK') {
      throw new Error(`startup assertion failed: title=${title}, heading=${heading}`);
    }
    return { ok: true, status: 'BROWSER_STARTUP_VERIFIED', title, heading };
  } finally {
    await context.close();
  }
}

async function runDirectMediaCanary(browser) {
  const context = await browser.newContext({
    viewport: { width: 1280, height: 720 },
    serviceWorkers: 'block',
  });
  const page = await context.newPage();
  attachDiagnostics(page, 'direct-media');
  const targetUrl = 'https://interactive-examples.mdn.mozilla.net/media/cc0-videos/flower.mp4';
  try {
    await page.goto(targetUrl, { waitUntil: 'domcontentloaded', timeout: 60_000 });
    await page.locator('video').first().waitFor({ state: 'attached', timeout: 30_000 });
    await startVideo(page);
    await page.waitForTimeout(2_000);
    const before = await videoState(page);
    await page.screenshot({ path: path.join(evidenceDir, 'direct-media-before.png') });
    await page.waitForTimeout(6_000);
    const after = await videoState(page);
    await page.screenshot({ path: path.join(evidenceDir, 'direct-media-after.png') });
    const advancedBy = after.currentTime - before.currentTime;
    if (advancedBy < 2) {
      throw new Error(`direct media did not advance enough: ${advancedBy.toFixed(3)}s`);
    }
    return {
      ok: true,
      status: 'PUBLIC_HTML5_VIDEO_PLAYBACK_VERIFIED',
      targetUrl,
      before,
      after,
      advancedBy,
    };
  } finally {
    await context.close();
  }
}

async function runYouTubeCanary(browser) {
  const context = await browser.newContext({
    viewport: { width: 1280, height: 720 },
    serviceWorkers: 'block',
    locale: 'en-US',
  });
  const page = await context.newPage();
  attachDiagnostics(page, 'youtube');
  const videoId = 'YE7VzlLtp-4';
  const targetUrl = `https://www.youtube-nocookie.com/embed/${videoId}?autoplay=1&mute=1&playsinline=1&controls=1`;
  try {
    await page.goto(targetUrl, { waitUntil: 'domcontentloaded', timeout: 90_000 });
    await page.screenshot({ path: path.join(evidenceDir, 'youtube-page-loaded.png') });

    const video = page.locator('video').first();
    await video.waitFor({ state: 'attached', timeout: 45_000 });

    const playButton = page.locator('.ytp-large-play-button, .ytp-play-button').first();
    if (await playButton.isVisible().catch(() => false)) {
      await playButton.click({ timeout: 10_000 }).catch(() => {});
    }
    await startVideo(page);
    await page.waitForTimeout(3_000);

    const before = await videoState(page);
    await page.screenshot({ path: path.join(evidenceDir, 'youtube-before.png') });
    await page.waitForTimeout(10_000);
    const after = await videoState(page);
    await page.screenshot({ path: path.join(evidenceDir, 'youtube-after.png') });

    const visibleError = await page.locator('.ytp-error, #error-screen, .ytp-error-content-wrap').first().textContent().catch(() => null);
    const advancedBy = after.currentTime - before.currentTime;
    if (visibleError?.trim()) {
      throw new Error(`YouTube displayed an error: ${visibleError.trim().slice(0, 500)}`);
    }
    if (advancedBy < 3) {
      throw new Error(`YouTube video did not advance enough: ${advancedBy.toFixed(3)}s`);
    }

    return {
      ok: true,
      status: 'PUBLIC_YOUTUBE_PLAYBACK_VERIFIED',
      videoId,
      targetUrl,
      before,
      after,
      advancedBy,
    };
  } finally {
    await context.close();
  }
}

let browser;
let fatalError = null;
const startedAt = new Date().toISOString();
try {
  browser = await chromium.launch(launchOptions());
  for (const [name, canary] of [
    ['startup', runStartupSmoke],
    ['directMedia', runDirectMediaCanary],
    ['youtube', runYouTubeCanary],
  ]) {
    try {
      const result = await canary(browser);
      results.push({ name, ...result });
    } catch (error) {
      const message = error instanceof Error ? `${error.name}: ${error.message}` : String(error);
      results.push({ name, ok: false, status: 'FAILED', error: message });
      fatalError ??= new Error(`${name} canary failed: ${message}`);
    }
  }
} catch (error) {
  fatalError = error instanceof Error ? error : new Error(String(error));
} finally {
  if (browser) {
    await browser.close().catch(() => {});
  }
}

const finalResult = {
  ok: !fatalError && results.every((result) => result.ok),
  status: !fatalError && results.every((result) => result.ok)
    ? 'MIRU_PUBLIC_BROWSER_RUNNER_VERIFIED'
    : 'MIRU_PUBLIC_BROWSER_RUNNER_FAILED',
  browserExecutablePath: process.env.BROWSER_EXECUTABLE_PATH || null,
  startedAt,
  completedAt: new Date().toISOString(),
  results,
  consoleEvents,
  pageErrors,
  fatalError: fatalError ? fatalError.message : null,
};

await writeJson('final-result.json', finalResult);
process.stdout.write(`${JSON.stringify(finalResult, null, 2)}\n`);
if (!finalResult.ok) {
  process.exitCode = 1;
}
