/**
 * 只读分享 API（FR-N-3）。
 *
 * POST /api/share         生成快照与 token
 * GET  /api/share/{token} 读取快照
 *
 * 「链接即凭据」——token 既是 PK 也是访问凭据。前端拿到 token 后拼成
 * /#/share/{token} 的前端路由，访问者无需登录。
 */

import { apiGet, apiSend } from './client';
import type { ShareCreateResponse, ShareReadResponse } from './types';

export function createShare(payload: {
  turn_id: number;
  session_id: string;
  expires_in_days?: number | null;
}) {
  return apiSend<ShareCreateResponse>('POST', '/api/share', {
    turn_id: payload.turn_id,
    session_id: payload.session_id,
    expires_in_days: payload.expires_in_days ?? null,
  });
}

export function readShare(token: string) {
  return apiGet<ShareReadResponse>(`/api/share/${encodeURIComponent(token)}`);
}

/** 把 token 拼成前端可访问的 hash 路由。前端用 HashRouter，前缀必须是 #/。 */
export function buildShareUrl(token: string): string {
  const base = `${window.location.origin}${window.location.pathname}`;
  return `${base}#/share/${encodeURIComponent(token)}`;
}
