#!/usr/bin/env node
import { chromium } from 'playwright';
import { lookup } from 'node:dns/promises';
import { isIP } from 'node:net';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import process from 'node:process';

function parseArgs(argv) {
  const result = {};
  for (let index = 2; index < argv.length; index += 2) {
    const key = argv[index];
    const value = argv[index + 1];
    if (!key?.startsWith('--') || value === undefined) throw new Error('arguments must be --key value pairs');
    result[key.slice(2)] = value;
  }
  if (!result.request || !result.evidence) throw new Error('--request and --evidence are required');
  return result;
}

function isPrivateIpv4(address) {
  const octets = address.split('.').map(Number);
  if (octets.length !== 4 || octets.some((value) => !Number.isInteger(value) || value < 0 || value > 255)) return true;
  const [a, b] = octets;
  return a === 0 || a === 10 || a === 127 || a >= 224 ||
    (a === 100 && b >= 64 && b <= 127) ||
    (a === 169 && b === 254) ||
    (a === 172 && b >= 16 && b <= 31) ||
    (a === 192 && (b === 0 || b === 168)) ||
    (a === 198 && (b === 18 || b === 19)) ||
    (a === 192 && b === 0) ||
    (a === 192 && b === 0 && octets[2] === 2) ||
    (a === 198 && b === 51 && octets[2] === 100) ||
    (a === 203 && b === 0 && octets[2] === 113);
}

function isPrivateIp(address) {
  const version = isIP(address);
  if (version === 4) return isPrivateIpv4(address);
  if (version !== 6) return true;
  const value = address.toLowerCase();
  if (value === '::' || value === '::1' || value.startsWith('fc') || value.startsWith('fd') ||
      value.startsWith('fe8') || value.startsWith('fe9') || value.startsWith('fea') || value.startsWith('feb') ||
      value.startsWith('ff') || value.startsWith('2001:db8:')) return true;
  if (value.startsWith('::ffff:')) return isPrivateIpv4(value.slice(7));
  return false;
}

function validateHostname(hostname) {
  const value = hostname.toLowerCase().replace(/\.$/, '');
  if (!value || value === 'localhost' || value.endsWith('.localhost') || value.endsWith('.local') ||
      value.endsWith('.internal') || value.endsWith('.lan') || value.endsWith('.home')) {
    throw new Error(`forbidden hostname: ${hostname}`);
  }
  if (isIP(value) && isPrivateIp(value)) throw new Error(`forbidden IP address: ${value}`);
  return value;
}

function parsePublicHttpsUrl(raw) {
  if (typeof raw !== 'string' || raw.length < 10 || raw.length > 2048) throw new Error('url length is invalid');
  const url = new URL(raw);
  if (url.protocol !== 'https:') throw new Error('only https URLs are allowed');
  if (url.username || url.password) throw new Error('URL credentials are forbidden');
  if (url.port && url.port !== '443') throw new Error('only HTTPS port 443 is allowed');
  validateHostname(url.hostname);
  return url;
}

async function resolvePublicHost(hostname) {
  const normalized = validateHostname(hostname);
  if (isIP(normalized)) return [normalized];
  const records = await lookup(normalized, { all: true, verbatim: true });
  if (!records.length) throw new Error(`DNS returned no addresses for ${normalized}`);
  for (const record of records) {
    if (isPrivateIp(record.address)) throw new Error(`hostname resolved to a forbidden address: ${normalized}`);
  }
  return records.map((record) => record.address);
}

function loadRequest(raw) {
  const request = JSON.parse(raw);
  const allowedKeys = new Set(['schemaVersion', 'requestId', 'url', 'waitMs', 'fullPage', 'maxTextChars']);
  for (const key of Object.keys(request)) if (!allowedKeys.has(key)) throw new Error(`unsupported request field: ${key}`);
  if (request.schemaVersion !== '1.0.0') throw new Error('unsupported schemaVersion');
  if (!/^PUBLIC-REQUEST-[A-Z0-9][A-Z0-9_-]{3,79}$/.test(request.requestId || '')) throw new Error('invalid requestId');
  const url = parsePublicHttpsUrl(request.url);
  const waitMs = request.waitMs ?? 2000;
  const maxTextChars = request.maxTextChars ?? 12000;
  if (!Number.isInteger(waitMs) || waitMs < 0 || waitMs > 10000) throw new Error('waitMs must be an integer from 0 to 10000');
  if (!Number.isInteger(maxTextChars) || maxTextChars < 1000 || maxTextChars > 30000) throw new Error('maxTextChars must be an integer from 1000 to 30000');
  if (typeof request.fullPage !== 'boolean') throw new Error('fullPage must be boolean');
  return { ...request, url: url.href, waitMs, maxTextChars };
}

const args = parseArgs(process.argv);
const evidenceDir = path.resolve(args.evidence);
await mkdir(evidenceDir, { recursive: true });
const requestRaw = await readFile(path.resolve(args.request), 'utf8');
const request = loadRequest(requestRaw);
const initialUrl = new URL(request.url);
await resolvePublicHost(initialUrl.hostname);

const executablePath = String(process.env.BROWSER_EXECUTABLE_PATH || '').trim();
let browser;
let context;
let traceStopped = false;
const requestLog = [];
const blockedRequests = [];
const dnsCache = new Map();
const resolveCached = (hostname) => {
  if (!dnsCache.has(hostname)) dnsCache.set(hostname, resolvePublicHost(hostname));
  return dnsCache.get(hostname);
};

let finalResult;
try {
  browser = await chromium.launch({
    headless: true,
    ...(executablePath ? { executablePath } : {}),
    args: ['--disable-dev-shm-usage', '--no-first-run'],
  });
  context = await browser.newContext({
    viewport: { width: 1440, height: 1000 },
    acceptDownloads: false,
    serviceWorkers: 'block',
  });
  await context.tracing.start({ screenshots: true, snapshots: true, sources: false });
  const page = await context.newPage();
  page.setDefaultNavigationTimeout(60_000);
  page.setDefaultTimeout(20_000);

  await page.route('**/*', async (route) => {
    const networkRequest = route.request();
    const record = {
      method: networkRequest.method(),
      url: networkRequest.url(),
      resourceType: networkRequest.resourceType(),
    };
    try {
      const url = parsePublicHttpsUrl(record.url);
      if (!['GET', 'HEAD'].includes(record.method)) throw new Error(`method blocked: ${record.method}`);
      if (requestLog.length >= 400) throw new Error('maximum request count exceeded');
      await resolveCached(url.hostname);
      requestLog.push(record);
      await route.continue();
    } catch (error) {
      blockedRequests.push({ ...record, reason: error instanceof Error ? error.message : String(error) });
      await route.abort('blockedbyclient');
    }
  });

  await page.goto(request.url, { waitUntil: 'domcontentloaded', timeout: 60_000 });
  await page.waitForTimeout(request.waitMs);
  const finalUrl = parsePublicHttpsUrl(page.url());
  await resolveCached(finalUrl.hostname);
  const screenshotPath = path.join(evidenceDir, 'page.png');
  await page.screenshot({ path: screenshotPath, fullPage: request.fullPage });
  const title = await page.title();
  const text = ((await page.locator('body').innerText().catch(() => '')) || '').trim().slice(0, request.maxTextChars);
  const tracePath = path.join(evidenceDir, 'trace.zip');
  await context.tracing.stop({ path: tracePath });
  traceStopped = true;
  finalResult = {
    ok: true,
    status: 'PUBLIC_BROWSER_REQUEST_VERIFIED',
    requestId: request.requestId,
    requestedUrl: request.url,
    finalUrl: finalUrl.href,
    title,
    text,
    observedRequestCount: requestLog.length,
    blockedRequestCount: blockedRequests.length,
    blockedRequests: blockedRequests.slice(0, 50),
    browserVersion: browser.version(),
    browserExecutablePath: executablePath || null,
    clickPerformed: false,
    formSubmitted: false,
    downloadPerformed: false,
    uploadPerformed: false,
    credentialsUsed: false,
    storageStateUsed: false,
    completedAt: new Date().toISOString(),
  };
} catch (error) {
  finalResult = {
    ok: false,
    status: 'PUBLIC_BROWSER_REQUEST_FAILED',
    requestId: request.requestId,
    requestedUrl: request.url,
    error: error instanceof Error ? `${error.name}: ${error.message}` : String(error),
    observedRequestCount: requestLog.length,
    blockedRequestCount: blockedRequests.length,
    blockedRequests: blockedRequests.slice(0, 50),
    completedAt: new Date().toISOString(),
  };
} finally {
  if (context && !traceStopped) {
    await context.tracing.stop({ path: path.join(evidenceDir, 'trace-failure.zip') }).catch(() => {});
  }
  if (context) await context.close().catch(() => {});
  if (browser) await browser.close().catch(() => {});
}

await writeFile(path.join(evidenceDir, 'final-result.json'), `${JSON.stringify(finalResult, null, 2)}\n`, 'utf8');
process.stdout.write(`${JSON.stringify(finalResult, null, 2)}\n`);
if (!finalResult.ok) process.exitCode = 1;
