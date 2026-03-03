import http from 'k6/http';
import { sleep } from 'k6';

import {
  buildUrl,
  boolEnv,
  checkStatus,
  normalizePath,
  numberEnv,
  parseApiKeys,
  pickApiKey,
  requiredEnv,
} from './lib/common.js';
import { makeIntegrationHmacHeaders } from './lib/hmac.js';

const BASE_URL = requiredEnv('BASE_URL');
const API_KEYS = parseApiKeys();

const TOKEN_PATH = normalizePath(
  __ENV.TOKEN_PATH || '/api/v1/integrations/token/',
);
const CLIENT_ID = requiredEnv('INTEGRATION_CLIENT_ID');
const CLIENT_SECRET_B64 = requiredEnv('INTEGRATION_CLIENT_SECRET_B64');

const RATE = numberEnv('TOKEN_RATE', 20);
const DURATION = __ENV.TOKEN_DURATION || '5m';
const PRE_ALLOCATED_VUS = numberEnv('TOKEN_PRE_ALLOCATED_VUS', 50);
const MAX_VUS = numberEnv('TOKEN_MAX_VUS', 200);
const SLEEP_SECONDS = numberEnv('SLEEP_SECONDS', 0);
const ALLOW_429 = boolEnv('ALLOW_429', false);

const ALLOWED_STATUSES = ALLOW_429 ? [200, 429] : [200];

export const options = {
  scenarios: {
    integration_token_hmac: {
      executor: 'constant-arrival-rate',
      rate: RATE,
      timeUnit: '1s',
      duration: DURATION,
      preAllocatedVUs: PRE_ALLOCATED_VUS,
      maxVUs: MAX_VUS,
    },
  },
  thresholds: {
    http_req_failed: ['rate<0.03'],
    http_req_duration: ['p(95)<1500'],
  },
};

export default function () {
  const apiKey = pickApiKey(API_KEYS);
  const { url, queryString } = buildUrl(BASE_URL, TOKEN_PATH, {});

  const method = 'POST';
  const timestamp = Math.floor(Date.now() / 1000);
  const nonce = `k6-${__VU}-${__ITER}-${Date.now()}`;
  const body = '';

  const hmacHeaders = makeIntegrationHmacHeaders({
    method,
    path: TOKEN_PATH,
    queryString,
    body,
    clientId: CLIENT_ID,
    secretB64: CLIENT_SECRET_B64,
    timestamp,
    nonce,
  });

  const response = http.post(url, body, {
    headers: {
      ...hmacHeaders,
      'X-API-Key': apiKey,
      'Content-Type': 'application/json',
      Accept: 'application/json',
      'User-Agent': 'k6-integration-token-load',
    },
    tags: {
      endpoint: 'integration_token',
      profile: 'hmac_token',
    },
  });

  checkStatus(response, ALLOWED_STATUSES, 'integration_token_hmac');

  if (SLEEP_SECONDS > 0) {
    sleep(SLEEP_SECONDS);
  }
}
