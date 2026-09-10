import { PROVIDERS, validateCookieHeader, validateTwitterHeader } from './cookies.js';

export const PROTOCOL_VERSION = 1;
export const NATIVE_HOST_NAME = 'com.jayden.rsshub_cookie_sync';

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
});

export function diagnosticReasonLabel(reason) {
  if (Object.hasOwn(DIAGNOSTIC_REASON_LABELS, reason)) return DIAGNOSTIC_REASON_LABELS[reason];
  if (typeof reason === 'string' && /^http_[1-5][0-9]{2}$/u.test(reason)) return `上游返回 HTTP ${reason.slice(5)}`;
  return sanitizeReason(reason) ?? '未提供具体原因（旧组件可能只返回结果）';
}

export function sanitizeReason(reason) {
  const allowed = new Set([
    'ok',
    'invalid_response',
    'native_host_unavailable',
    'permission_denied',
    'missing_cookie',
    'candidate_invalid',
    'upstream_temporary_failure',
    'server_rejected',
  ]);
  return typeof reason === 'string' && (allowed.has(reason) || Object.hasOwn(DIAGNOSTIC_REASON_LABELS, reason) || /^http_[1-5][0-9]{2}$/u.test(reason)) ? reason : undefined;
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

export function sanitizeDiagnosticLog(raw) {
  if (!Array.isArray(raw)) return [];
  const stages = new Set(['collect', 'native', 'complete']);
  const statuses = new Set([...HOST_RESULT_STATUSES, 'started', 'collected', 'permission_required', 'missing_cookie']);
  const localReasons = new Set(['cookie_read_failed', 'permission_check_failed', 'malformed_cookie', 'hashing_failed']);
  return raw.slice(-200).filter((entry) => entry && PROVIDERS.includes(entry.provider) && stages.has(entry.stage) && statuses.has(entry.status) && Number.isFinite(entry.time) && entry.time > 0).map((entry) => ({
    time: entry.time,
    provider: entry.provider,
    trigger: ['manual', 'automatic'].includes(entry.trigger) ? entry.trigger : 'automatic',
    stage: entry.stage,
    status: entry.status,
    reason: sanitizeReason(entry.reason) ?? (localReasons.has(entry.reason) ? entry.reason : null),
    durationMs: Number.isFinite(entry.durationMs) && entry.durationMs >= 0 ? Math.min(Math.round(entry.durationMs), 86400000) : null,
  }));
}
