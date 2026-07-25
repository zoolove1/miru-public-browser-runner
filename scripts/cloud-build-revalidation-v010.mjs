#!/usr/bin/env node

import fs from 'node:fs';
import path from 'node:path';

const PROJECT_ID = 'autonomy-system';
const PROJECT_NUMBER = '770537155218';
const CLOUD_BUILD_SERVICE = 'cloudbuild.googleapis.com';
const REQUIRED_PERMISSIONS = [
  'cloudbuild.builds.create',
  'cloudbuild.builds.get',
  'cloudbuild.builds.list',
];

function fail(message, outputPath, details = {}) {
  if (outputPath) {
    writeJson(outputPath, {
      ok: false,
      status: 'CLOUD_BUILD_REVALIDATION_BLOCKED',
      projectId: PROJECT_ID,
      projectNumber: PROJECT_NUMBER,
      reason: String(message).slice(0, 300),
      secretMaterialReturned: false,
      completedAt: new Date().toISOString(),
      ...details,
    });
  }
  console.error(`[cloud-build-revalidation] ${message}`);
  process.exit(2);
}

function parseArgs(argv) {
  const values = new Map();
  for (let index = 0; index < argv.length; index += 2) {
    const key = argv[index];
    const value = argv[index + 1];
    if (!key?.startsWith('--') || value === undefined) throw new Error('Arguments must be --key value pairs');
    values.set(key.slice(2), value);
  }
  return values;
}

function readJson(filePath) {
  return JSON.parse(fs.readFileSync(filePath, 'utf8'));
}

function writeJson(filePath, value) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.writeFileSync(filePath, `${JSON.stringify(value, null, 2)}\n`);
}

function normalizeKey(value) {
  return String(value || '').toLowerCase().replace(/[^a-z0-9]/g, '');
}

function findAccessToken(root) {
  const seen = new Set();
  let found = '';
  function walk(value) {
    if (found || !value || typeof value !== 'object' || seen.has(value)) return;
    seen.add(value);
    for (const [key, item] of Object.entries(value)) {
      if (normalizeKey(key) === 'accesstoken' && typeof item === 'string' && item.length >= 20) {
        found = item;
        return;
      }
    }
    for (const item of Object.values(value)) walk(item);
  }
  walk(root);
  return found;
}

function sanitizeError(status, parsed) {
  const items = Array.isArray(parsed?.error?.errors) ? parsed.error.errors : [];
  return {
    httpStatus: status,
    code: Number(parsed?.error?.code || status),
    status: String(parsed?.error?.status || '').slice(0, 100),
    message: String(parsed?.error?.message || 'unknown API error').slice(0, 500),
    reasons: [...new Set(items.map((item) => String(item?.reason || '')).filter(Boolean))].sort(),
  };
}

async function requestJson(method, url, token, body = undefined) {
  const response = await fetch(url, {
    method,
    headers: {
      authorization: `Bearer ${token}`,
      ...(body === undefined ? {} : { 'content-type': 'application/json' }),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
    redirect: 'error',
  });
  const text = await response.text();
  let parsed = {};
  try {
    parsed = text ? JSON.parse(text) : {};
  } catch (_) {
    return {
      ok: false,
      status: response.status,
      error: { httpStatus: response.status, code: response.status, status: '', message: 'non-JSON API response', reasons: [] },
    };
  }
  if (!response.ok) return { ok: false, status: response.status, error: sanitizeError(response.status, parsed) };
  return { ok: true, status: response.status, value: parsed };
}

function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function pollOperation(name, token) {
  const operationUrl = `https://cloudbuild.googleapis.com/v1/${name}`;
  for (let attempt = 0; attempt < 60; attempt += 1) {
    const result = await requestJson('GET', operationUrl, token);
    if (!result.ok) return { ok: false, error: result.error };
    if (result.value?.done) return { ok: true, operation: result.value };
    await sleep(3000);
  }
  return {
    ok: false,
    error: { httpStatus: 408, code: 408, status: 'TIMEOUT', message: 'Cloud Build operation did not finish within 180 seconds', reasons: [] },
  };
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const projectId = args.get('project');
  const clasprcPath = args.get('clasprc');
  const outputPath = args.get('output');
  if (projectId !== PROJECT_ID) fail(`Project must be exactly ${PROJECT_ID}`, outputPath);
  if (!clasprcPath || !outputPath) fail('Missing --clasprc or --output', outputPath);

  let credential;
  try {
    credential = readJson(clasprcPath);
  } catch (error) {
    fail(`Cannot read refreshed clasp credential: ${error.message}`, outputPath);
  }
  const token = findAccessToken(credential);
  if (!token) fail('Refreshed clasp credential contains no access token', outputPath);

  const serviceResult = await requestJson(
    'GET',
    `https://serviceusage.googleapis.com/v1/projects/${PROJECT_NUMBER}/services/${CLOUD_BUILD_SERVICE}`,
    token,
  );
  const billingResult = await requestJson(
    'GET',
    `https://cloudbilling.googleapis.com/v1/projects/${PROJECT_ID}/billingInfo`,
    token,
  );
  const permissionResult = await requestJson(
    'POST',
    `https://cloudresourcemanager.googleapis.com/v1/projects/${PROJECT_ID}:testIamPermissions`,
    token,
    { permissions: REQUIRED_PERMISSIONS },
  );
  const recentBuildsResult = await requestJson(
    'GET',
    `https://cloudbuild.googleapis.com/v1/projects/${PROJECT_ID}/builds?pageSize=5`,
    token,
  );

  const serviceState = serviceResult.ok ? String(serviceResult.value?.state || '') : 'UNKNOWN';
  const billingEnabled = billingResult.ok ? Boolean(billingResult.value?.billingEnabled) : null;
  const grantedPermissions = permissionResult.ok
    ? (Array.isArray(permissionResult.value?.permissions) ? permissionResult.value.permissions.map(String).sort() : [])
    : [];
  const recentBuildCount = recentBuildsResult.ok
    ? (Array.isArray(recentBuildsResult.value?.builds) ? recentBuildsResult.value.builds.length : 0)
    : null;

  if (!serviceResult.ok || serviceState !== 'ENABLED') {
    fail('Cloud Build API is not confirmed ENABLED', outputPath, {
      api: { serviceState, error: serviceResult.ok ? null : serviceResult.error },
      billing: { billingEnabled, error: billingResult.ok ? null : billingResult.error },
      permissions: { granted: grantedPermissions, error: permissionResult.ok ? null : permissionResult.error },
      recentBuilds: { count: recentBuildCount, error: recentBuildsResult.ok ? null : recentBuildsResult.error },
      mutationPerformed: false,
    });
  }

  const buildBody = {
    steps: [
      {
        name: 'gcr.io/cloud-builders/gcloud',
        entrypoint: 'bash',
        args: ['-ceu', "printf 'CLOUD_BUILD_REVALIDATION_OK\\n'"],
      },
    ],
    timeout: '120s',
    options: {
      logging: 'CLOUD_LOGGING_ONLY',
      machineType: 'E2_MEDIUM',
    },
    tags: ['miru-cloud-build-revalidation-v010'],
  };

  const createResult = await requestJson(
    'POST',
    `https://cloudbuild.googleapis.com/v1/projects/${PROJECT_ID}/builds`,
    token,
    buildBody,
  );
  if (!createResult.ok) {
    fail('Cloud Build no-op create request was rejected', outputPath, {
      api: { serviceState, error: null },
      billing: { billingEnabled, error: billingResult.ok ? null : billingResult.error },
      permissions: { granted: grantedPermissions, error: permissionResult.ok ? null : permissionResult.error },
      recentBuilds: { count: recentBuildCount, error: recentBuildsResult.ok ? null : recentBuildsResult.error },
      buildCreateError: createResult.error,
      mutationPerformed: false,
    });
  }

  const operationName = String(createResult.value?.name || '');
  if (!operationName.startsWith('operations/')) {
    fail('Cloud Build create response did not return an operation name', outputPath, {
      api: { serviceState },
      mutationPerformed: true,
    });
  }

  const polled = await pollOperation(operationName, token);
  if (!polled.ok) {
    fail('Cloud Build no-op operation could not be verified', outputPath, {
      api: { serviceState },
      billing: { billingEnabled, error: billingResult.ok ? null : billingResult.error },
      permissions: { granted: grantedPermissions, error: permissionResult.ok ? null : permissionResult.error },
      operationName,
      operationError: polled.error,
      mutationPerformed: true,
    });
  }

  const operation = polled.operation;
  if (operation?.error) {
    fail('Cloud Build no-op operation completed with an error', outputPath, {
      api: { serviceState },
      operationName,
      operationError: sanitizeError(Number(operation.error.code || 500), { error: operation.error }),
      mutationPerformed: true,
    });
  }

  const build = operation?.response || operation?.metadata?.build || {};
  const buildStatus = String(build?.status || operation?.metadata?.build?.status || 'UNKNOWN');
  const buildId = String(build?.id || operation?.metadata?.build?.id || '');
  const success = buildStatus === 'SUCCESS';

  writeJson(outputPath, {
    ok: success,
    status: success ? 'CLOUD_BUILD_REVALIDATED' : 'CLOUD_BUILD_REVALIDATION_BUILD_NOT_SUCCESSFUL',
    projectId: PROJECT_ID,
    projectNumber: PROJECT_NUMBER,
    api: { service: CLOUD_BUILD_SERVICE, serviceState, readbackOk: serviceResult.ok },
    billing: { billingEnabled, readbackOk: billingResult.ok, error: billingResult.ok ? null : billingResult.error },
    permissions: {
      requested: REQUIRED_PERMISSIONS,
      granted: grantedPermissions,
      readbackOk: permissionResult.ok,
      error: permissionResult.ok ? null : permissionResult.error,
    },
    recentBuilds: {
      count: recentBuildCount,
      readbackOk: recentBuildsResult.ok,
      error: recentBuildsResult.ok ? null : recentBuildsResult.error,
    },
    canary: {
      buildId,
      buildStatus,
      operationName,
      sourceAttached: false,
      artifactsConfigured: false,
      timeoutSeconds: 120,
      expectedMarker: 'CLOUD_BUILD_REVALIDATION_OK',
    },
    mutationPerformed: true,
    mutationScope: 'one source-free no-op Cloud Build execution',
    secretMaterialReturned: false,
    completedAt: new Date().toISOString(),
  });

  if (!success) process.exit(3);
  console.log(`[cloud-build-revalidation] verified: buildId=${buildId}, status=${buildStatus}`);
}

await main();
