import crypto from 'k6/crypto';
import encoding from 'k6/encoding';

function encodeRFC3986(value) {
  return encodeURIComponent(value).replace(/[!'()*]/g, (char) =>
    `%${char.charCodeAt(0).toString(16).toUpperCase()}`,
  );
}

function decodeQueryComponent(value) {
  try {
    return decodeURIComponent(value.replace(/\+/g, ' '));
  } catch (_) {
    return value;
  }
}

export function canonicalizeQuery(queryString) {
  if (!queryString) {
    return '';
  }

  const pairs = [];
  for (const part of queryString.split('&')) {
    if (!part) {
      continue;
    }
    const idx = part.indexOf('=');
    if (idx === -1) {
      pairs.push([decodeQueryComponent(part), '']);
      continue;
    }
    const key = part.slice(0, idx);
    const value = part.slice(idx + 1);
    pairs.push([decodeQueryComponent(key), decodeQueryComponent(value)]);
  }

  const encoded = pairs.map(([key, value]) => [
    encodeRFC3986(key),
    encodeRFC3986(value),
  ]);
  encoded.sort((a, b) => {
    if (a[0] === b[0]) {
      if (a[1] === b[1]) {
        return 0;
      }
      return a[1] < b[1] ? -1 : 1;
    }
    return a[0] < b[0] ? -1 : 1;
  });

  return encoded.map(([k, v]) => `${k}=${v}`).join('&');
}

function bodySha256Hex(method, body) {
  const normalizedMethod = method.toUpperCase();
  const payload = normalizedMethod === 'GET' ? '' : (body || '');
  return crypto.sha256(payload, 'hex');
}

function toBytesFromBase64(secretB64) {
  const raw = encoding.b64decode(secretB64, 'std');
  const bytes = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i += 1) {
    bytes[i] = raw.charCodeAt(i);
  }
  return bytes;
}

export function buildCanonicalString({
  method,
  path,
  queryString,
  timestamp,
  nonce,
  body,
}) {
  return [
    method.toUpperCase(),
    path,
    canonicalizeQuery(queryString || ''),
    `${timestamp}`,
    nonce,
    bodySha256Hex(method, body || ''),
  ].join('\n');
}

export function computeIntegrationSignatureHex({
  secretB64,
  canonicalString,
}) {
  const secretBytes = toBytesFromBase64(secretB64);
  return crypto.hmac('sha256', secretBytes.buffer, canonicalString, 'hex');
}

export function makeIntegrationHmacHeaders({
  method,
  path,
  queryString,
  body,
  clientId,
  secretB64,
  timestamp,
  nonce,
}) {
  const canonicalString = buildCanonicalString({
    method,
    path,
    queryString,
    timestamp,
    nonce,
    body,
  });
  const signature = computeIntegrationSignatureHex({
    secretB64,
    canonicalString,
  });

  return {
    'X-Client-Id': clientId,
    'X-Timestamp': `${timestamp}`,
    'X-Nonce': nonce,
    'X-Signature': signature,
  };
}
