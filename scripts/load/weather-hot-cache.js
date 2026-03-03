import http from 'k6/http';
import { sleep } from 'k6';

import {
  buildUrl,
  checkStatus,
  endpointPathEnv,
  numberEnv,
  parseApiKeys,
  pickApiKey,
  requiredEnv,
} from './lib/common.js';

const BASE_URL = requiredEnv('BASE_URL');
const API_KEYS = parseApiKeys();

const API_PATH = endpointPathEnv(
  'API_PATH',
  'PATH',
  '/api/v1/weather/current/',
);
const LAT = __ENV.LAT || '-1.2864';
const LON = __ENV.LON || '36.8172';
const TZ = __ENV.TZ || 'Africa/Nairobi';
const PROVIDER = __ENV.PROVIDER || '';

const RATE = numberEnv('HOT_RATE', 40);
const DURATION = __ENV.HOT_DURATION || '5m';
const PRE_ALLOCATED_VUS = numberEnv('HOT_PRE_ALLOCATED_VUS', 80);
const MAX_VUS = numberEnv('HOT_MAX_VUS', 300);
const SLEEP_SECONDS = numberEnv('SLEEP_SECONDS', 0);

export const options = {
  scenarios: {
    hot_cache: {
      executor: 'constant-arrival-rate',
      rate: RATE,
      timeUnit: '1s',
      duration: DURATION,
      preAllocatedVUs: PRE_ALLOCATED_VUS,
      maxVUs: MAX_VUS,
    },
  },
  thresholds: {
    http_req_failed: ['rate<0.01'],
    http_req_duration: ['p(95)<800'],
  },
};

export default function () {
  const apiKey = pickApiKey(API_KEYS);
  const { url } = buildUrl(BASE_URL, API_PATH, {
    lat: LAT,
    lon: LON,
    tz: TZ,
    provider: PROVIDER,
  });

  const response = http.get(url, {
    headers: {
      'X-API-Key': apiKey,
      Accept: 'application/json',
    },
    tags: {
      endpoint: 'weather_current',
      profile: 'hot_cache',
    },
  });

  checkStatus(response, [200], 'weather_hot_cache');

  if (SLEEP_SECONDS > 0) {
    sleep(SLEEP_SECONDS);
  }
}
