import { check } from 'k6';

export function requiredEnv(name) {
  const value = __ENV[name];
  if (!value) {
    throw new Error(`Missing required env var: ${name}`);
  }
  return value;
}

export function numberEnv(name, fallback) {
  const raw = __ENV[name];
  if (!raw) {
    return fallback;
  }
  const value = Number(raw);
  if (!Number.isFinite(value)) {
    throw new Error(`Invalid numeric env var ${name}: ${raw}`);
  }
  return value;
}

export function boolEnv(name, fallback) {
  const raw = __ENV[name];
  if (raw === undefined) {
    return fallback;
  }
  return ["1", "true", "yes", "on"].includes(raw.toLowerCase());
}

export function parseApiKeys() {
  const list = (__ENV.API_KEY_LIST || "")
    .split(",")
    .map((item) => item.trim())
    .filter((item) => item.length > 0);
  if (list.length > 0) {
    return list;
  }
  const single = (__ENV.API_KEY || "").trim();
  if (!single) {
    throw new Error("Set API_KEY or API_KEY_LIST");
  }
  return [single];
}

export function pickApiKey(apiKeys) {
  if (apiKeys.length === 1) {
    return apiKeys[0];
  }
  const idx = ((__VU - 1) + __ITER) % apiKeys.length;
  return apiKeys[idx];
}

export function buildUrl(baseUrl, path, queryParams) {
  const base = baseUrl.endsWith("/") ? baseUrl.slice(0, -1) : baseUrl;
  const cleanPath = normalizePath(path);
  const entries = Object.entries(queryParams || {}).filter(
    ([, value]) => value !== undefined && value !== null && `${value}`.length > 0,
  );
  if (entries.length === 0) {
    return { url: `${base}${cleanPath}`, queryString: "" };
  }

  const encoded = entries.map(([key, value]) => {
    const k = encodeURIComponent(`${key}`);
    const v = encodeURIComponent(`${value}`);
    return `${k}=${v}`;
  });
  const queryString = encoded.join("&");
  return {
    url: `${base}${cleanPath}?${queryString}`,
    queryString,
  };
}

export function normalizePath(path) {
  if (!path || path === "") {
    return "/";
  }
  return path.startsWith("/") ? path : `/${path}`;
}

export function endpointPathEnv(preferredName, legacyName, fallbackPath) {
  const preferred = (__ENV[preferredName] || "").trim();
  if (preferred) {
    return normalizePath(preferred);
  }

  const legacy = (__ENV[legacyName] || "").trim();
  if (!legacy) {
    return normalizePath(fallbackPath);
  }

  // Ignore shell PATH-like values (for example: /usr/bin:/bin).
  if (legacy.includes(":")) {
    return normalizePath(fallbackPath);
  }

  return normalizePath(legacy);
}

export function randomInRange(min, max, decimals) {
  const value = min + Math.random() * (max - min);
  return value.toFixed(decimals);
}

export function checkStatus(response, allowedStatuses, label) {
  check(response, {
    [`${label} status in ${allowedStatuses.join(',')}`]: (res) =>
      allowedStatuses.includes(res.status),
  });
}
