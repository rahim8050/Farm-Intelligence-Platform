import http from 'k6/http';
import { sleep } from 'k6';

import {
  buildUrl,
  checkStatus,
  endpointPathEnv,
  numberEnv,
  parseApiKeys,
  pickApiKey,
  randomInRange,
  requiredEnv,
} from './lib/common.js';

const BASE_URL = requiredEnv('BASE_URL');
const API_KEYS = parseApiKeys();

const API_PATH = endpointPathEnv(
  'API_PATH',
  'PATH',
  '/api/v1/weather/current/',
);
const TZ = __ENV.TZ || 'Africa/Nairobi';
const PROVIDERS = (__ENV.PROVIDERS || 'open_meteo,nasa_power')
  .split(',')
  .map((item) => item.trim())
  .filter((item) => item.length > 0);

const MIN_LAT = numberEnv('MIN_LAT', -1.6);
const MAX_LAT = numberEnv('MAX_LAT', -1.0);
const MIN_LON = numberEnv('MIN_LON', 36.6);
const MAX_LON = numberEnv('MAX_LON', 37.0);

const RATE = numberEnv('MIXED_RATE', 25);
const DURATION = __ENV.MIXED_DURATION || '5m';
const PRE_ALLOCATED_VUS = numberEnv('MIXED_PRE_ALLOCATED_VUS', 60);
const MAX_VUS = numberEnv('MIXED_MAX_VUS', 250);
const SLEEP_SECONDS = numberEnv('SLEEP_SECONDS', 0);

export const options = {
  scenarios: {
    mixed_cache: {
      executor: 'constant-arrival-rate',
      rate: RATE,
      timeUnit: '1s',
      duration: DURATION,
      preAllocatedVUs: PRE_ALLOCATED_VUS,
      maxVUs: MAX_VUS,
    },
  },
  thresholds: {
    http_req_failed: ['rate<0.02'],
    http_req_duration: ['p(95)<1200'],
  },
};

function pickProvider() {
  if (PROVIDERS.length === 0) {
    return '';
  }
  const idx = ((__VU - 1) + __ITER) % PROVIDERS.length;
  return PROVIDERS[idx];
}

export default function () {
  const apiKey = pickApiKey(API_KEYS);
  const provider = pickProvider();

  const { url } = buildUrl(BASE_URL, API_PATH, {
    lat: randomInRange(MIN_LAT, MAX_LAT, 4),
    lon: randomInRange(MIN_LON, MAX_LON, 4),
    tz: TZ,
    provider,
  });

  const response = http.get(url, {
    headers: {
      'X-API-Key': apiKey,
      Accept: 'application/json',
    },
    tags: {
      endpoint: 'weather_current',
      profile: 'mixed_cache',
    },
  });

  checkStatus(response, [200], 'weather_mixed_cache');

  if (SLEEP_SECONDS > 0) {
    sleep(SLEEP_SECONDS);
  }
}
