import { PROVIDERS, validateCookieHeader, validateTwitterHeader } from './cookies.js';

export const PROTOCOL_VERSION = 1;
export const NATIVE_HOST_NAME = 'io.github.jaaayden.rsshub_cookie_sync';

export const HOST_RESULT_STATUSES = Object.freeze([
  'unchanged',
  'candidate_saved',
  'promoted',
  'rejected_invalid',
  'retryable_error',
]);

const STATUS_SET = new Set(HOST_RESULT_STATUSES);

export function createSyncPayload(providerHeaders) {
  if (!providerHeaders || typeof providerHeaders !== 'object') {
    throw new TypeError('providerHeaders must be an object');
  }

  const providers = {};
  for (const provider of PROVIDERS) {
    const header = providerHeaders[provider];
    if (header === undefined) continue;
    validateCookieHeader(header);
    if (provider === 'twitter') validateTwitterHeader(header);
    providers[provider] = { cookieHeader: header };
  }
  if (Object.keys(providers).length === 0) {
    throw new TypeError('at least one provider is required');
  }
  return { version: PROTOCOL_VERSION, providers };
}

export function isKnownHostStatus(status) {
  return typeof status === 'string' && STATUS_SET.has(status);
}

export const DIAGNOSTIC_REASON_LABELS = Object.freeze({
  direct_sync: '直接同步，未验证上游登录态',
  host_configuration_invalid: '本机 Host 配置或文件权限异常',
  ssh_timeout: 'SSH 或远程处理超时',
  ssh_auth_failed: 'SSH 公钥认证失败',
  ssh_host_key_failed: 'SSH 主机指纹校验失败',
  ssh_connection_failed: 'SSH 连接失败',
  server_error: '远程程序执行失败，请检查服务器状态',
  remote_invalid_response: '远程响应格式异常，请确认三端版本一致',
  network_error: '服务器访问上游网络失败',
  twitter_csrf_missing: 'X 首页未返回可用 CSRF Cookie',
  twitter_invalid_response: 'X 账户接口响应无法确认登录态',
  twitter_auth_failed: 'X 登录凭证失效',
  twitter_token_pool_unsupported: '已有多账号令牌池，未自动接管',
  profile_missing: '账户接口未返回有效资料',
  profile_unauthorized: '账户接口拒绝登录态',
  moments_invalid_json: '动态接口返回非 JSON',
  moments_unauthorized: '动态接口拒绝登录态',
  config_invalid_json: '微博接口返回非 JSON',
  config_unauthorized: '微博接口拒绝登录态',
  config_not_ok: '微博登录检查未通过',
  config_logged_out: '微博已退出登录',
  invalid_cookie: '凭证格式异常',
  zhihu_missing_dc0: '知乎缺少 d_c0，请确认站点权限并在同一浏览器配置中登录后重试',
  zhihu_missing_zc0: '知乎缺少 z_c0，请在同一浏览器配置中登录后重试',
  zhihu_ambiguous_session: '知乎存在冲突的登录或设备 Cookie，请在同一浏览器配置中重新登录',
});

export function diagnosticReasonLabel(reason) {
  if (Object.hasOwn(DIAGNOSTIC_REASON_LABELS, reason)) return DIAGNOSTIC_REASON_LABELS[reason];
  if (typeof reason === 'string' && HTTP_REASON_RE.test(reason)) return `上游返回 HTTP ${reason.slice(5)}`;
  return sanitizeReason(reason) ?? '未提供具体原因（旧组件可能只返回结果）';
}

// Reasons the native host or the server may report, and reasons the extension
// itself records when a local step fails.  Single source of truth: state.js and
// the diagnostic log sanitizer use the same sets.
export const HOST_REASONS = Object.freeze([
  'ok',
  'invalid_response',
  'native_host_unavailable',
  'permission_denied',
  'missing_cookie',
  'candidate_invalid',
  'upstream_temporary_failure',
  'server_rejected',
]);
export const LOCAL_REASONS = Object.freeze([
  'cookie_read_failed',
  'permission_check_failed',
  'malformed_cookie',
  'hashing_failed',
]);

const HOST_REASON_SET = new Set(HOST_REASONS);
const LOCAL_REASON_SET = new Set(LOCAL_REASONS);
const HTTP_REASON_RE = /^http_[1-5][0-9]{2}$/u;

export function isLocalReason(reason) {
  return typeof reason === 'string' && LOCAL_REASON_SET.has(reason);
}

export function sanitizeReason(reason) {
  if (typeof reason !== 'string') return undefined;
  const known =
    HOST_REASON_SET.has(reason) || Object.hasOwn(DIAGNOSTIC_REASON_LABELS, reason) || HTTP_REASON_RE.test(reason);
  return known ? reason : undefined;
}

/**
 * Only copy the finite protocol vocabulary into extension storage/UI. A
 * native host must never be able to make arbitrary text (which could contain
 * a Cookie or response body) persistent.
 */
export function sanitizeHostResult(result, provider) {
  const candidate =
    result && typeof result === 'object' && result.providers && provider
      ? result.providers[provider]
      : result;
  if (!candidate || typeof candidate !== 'object') {
    return { status: 'retryable_error', reason: 'invalid_response' };
  }
  const status = isKnownHostStatus(candidate.status)
    ? candidate.status
    : 'retryable_error';
  const reason = sanitizeReason(candidate.reason);
  if (!isKnownHostStatus(candidate.status)) {
    return { status, reason: 'invalid_response' };
  }
  return reason ? { status, reason } : { status };
}

const DIAGNOSTIC_STAGES = new Set(['collect', 'native', 'complete']);
const DIAGNOSTIC_STATUSES = new Set([
  ...HOST_RESULT_STATUSES,
  'started',
  'collected',
  'permission_required',
  'missing_cookie',
]);
const MAX_DIAGNOSTIC_ENTRIES = 200;
const MAX_DIAGNOSTIC_DURATION_MS = 24 * 60 * 60 * 1000;

function isValidDiagnosticEntry(entry) {
  return Boolean(
    entry &&
      PROVIDERS.includes(entry.provider) &&
      DIAGNOSTIC_STAGES.has(entry.stage) &&
      DIAGNOSTIC_STATUSES.has(entry.status) &&
      Number.isFinite(entry.time) &&
      entry.time > 0,
  );
}

function sanitizeDiagnosticEntry(entry) {
  const duration = Number.isFinite(entry.durationMs) && entry.durationMs >= 0
    ? Math.min(Math.round(entry.durationMs), MAX_DIAGNOSTIC_DURATION_MS)
    : null;
  return {
    time: entry.time,
    provider: entry.provider,
    trigger: entry.trigger === 'manual' ? 'manual' : 'automatic',
    stage: entry.stage,
    status: entry.status,
    reason: sanitizeReason(entry.reason) ?? (isLocalReason(entry.reason) ? entry.reason : null),
    durationMs: duration,
  };
}

export function sanitizeDiagnosticLog(raw) {
  if (!Array.isArray(raw)) return [];
  return raw.slice(-MAX_DIAGNOSTIC_ENTRIES).filter(isValidDiagnosticEntry).map(sanitizeDiagnosticEntry);
}
