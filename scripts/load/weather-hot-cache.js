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

function parseArrivalRate(name, fallbackPerSecond) {
  const raw = (__ENV[name] || `${fallbackPerSecond}`).trim();
  const perSecond = Number(raw);
  if (!Number.isFinite(perSecond) || perSecond <= 0) {
    throw new Error(
      `Invalid ${name}=${raw}. Use a positive number (examples: 1, 2, 5, 0.5).`,
    );
  }
  if (Number.isInteger(perSecond)) {
    return { rate: perSecond, timeUnit: '1s' };
  }
  return {
    rate: Math.max(1, Math.round(perSecond * 60)),
    timeUnit: '1m',
  };
}

const ARRIVAL = parseArrivalRate('HOT_RATE', 40);
const DURATION = __ENV.HOT_DURATION || '5m';
const PRE_ALLOCATED_VUS = Math.max(
  1,
  Math.trunc(numberEnv('HOT_PRE_ALLOCATED_VUS', 80)),
);
const MAX_VUS = Math.max(1, Math.trunc(numberEnv('HOT_MAX_VUS', 300)));
const SLEEP_SECONDS = numberEnv('SLEEP_SECONDS', 0);
const REQUEST_TIMEOUT = __ENV.REQUEST_TIMEOUT || '30s';
const HOT_WARMUP_REQUESTS = Math.max(
  0,
  Math.trunc(numberEnv('HOT_WARMUP_REQUESTS', 0)),
);
const HOT_WARMUP_SLEEP_MS = Math.max(
  0,
  Math.trunc(numberEnv('HOT_WARMUP_SLEEP_MS', 0)),
);

export const options = {
  scenarios: {
    hot_cache: {
      executor: 'constant-arrival-rate',
      rate: ARRIVAL.rate,
      timeUnit: ARRIVAL.timeUnit,
      duration: DURATION,
      preAllocatedVUs: PRE_ALLOCATED_VUS,
      maxVUs: MAX_VUS,
    },
  },
  thresholds: {
    'http_req_failed{profile:hot_cache,phase:steady}': ['rate<0.01'],
    'http_req_duration{profile:hot_cache,phase:steady}': ['p(95)<800'],
  },
};

function buildHotUrl() {
  const { url } = buildUrl(BASE_URL, API_PATH, {
    lat: LAT,
    lon: LON,
    tz: TZ,
    provider: PROVIDER,
  });
  return url;
}

function fetchWeatherCurrent(apiKey, phaseTag) {
  const url = buildHotUrl();
  return http.get(url, {
    headers: {
      'X-API-Key': apiKey,
      Accept: 'application/json',
    },
    timeout: REQUEST_TIMEOUT,
    tags: {
      endpoint: 'weather_current',
      profile: 'hot_cache',
      phase: phaseTag,
    },
  });
}

export function setup() {
  if (HOT_WARMUP_REQUESTS <= 0) {
    return;
  }

  const warmupApiKey = API_KEYS[0];
  for (let i = 0; i < HOT_WARMUP_REQUESTS; i += 1) {
    const response = fetchWeatherCurrent(warmupApiKey, 'warmup');
    checkStatus(response, [200], 'weather_hot_cache_warmup');
    if (HOT_WARMUP_SLEEP_MS > 0) {
      sleep(HOT_WARMUP_SLEEP_MS / 1000);
    }
  }
}

export default function () {
  const apiKey = pickApiKey(API_KEYS);
  const response = fetchWeatherCurrent(apiKey, 'steady');

  checkStatus(response, [200], 'weather_hot_cache');

  if (SLEEP_SECONDS > 0) {
    sleep(SLEEP_SECONDS);
  }
}
