/**
 * 异常订阅接口封装（FR-O-1 ~ FR-O-5）。
 * 路由前缀 /api/alert，详见 backend/app/api/alerts.py。
 */

import { apiGet, apiSend } from './client';
import type {
  AlertCheckResponse,
  AlertEvent,
  AlertEventsResponse,
  AlertRule,
  AlertRuleBody,
  AlertRulesResponse,
  AlertSeenResponse,
} from './types';

export function fetchAlertRules(enabledOnly = false): Promise<AlertRule[]> {
  return apiGet<AlertRulesResponse>('/api/alert/rules', {
    enabled: enabledOnly ? 'true' : undefined,
  }).then((res) => res.rules);
}

/** id 缺省新建，带 id 更新（后端对不存在的 id 返回 404）。 */
export function upsertAlertRule(body: AlertRuleBody): Promise<AlertRule> {
  return apiSend<AlertRule>('POST', '/api/alert/rules', body);
}

export function deleteAlertRule(id: number): Promise<{ deleted: number }> {
  return apiSend('DELETE', `/api/alert/rules/${id}`);
}

/**
 * 立即求值。rule_ids 缺省求值全部启用规则；evaluatedAt 缺省由后端按
 * 周期量化当前时间。同一周期重复调用幂等，返回的是同一条事件记录。
 */
export function runAlertCheck(
  ruleIds?: number[],
  evaluatedAt?: string,
): Promise<AlertCheckResponse> {
  return apiSend<AlertCheckResponse>('POST', '/api/alert/check', {
    rule_ids: ruleIds ?? null,
    evaluated_at: evaluatedAt ?? null,
  });
}

export function fetchAlertEvents(params: {
  seen?: boolean;
  ruleId?: number;
  limit?: number;
}): Promise<AlertEventsResponse> {
  return apiGet<AlertEventsResponse>('/api/alert/events', {
    seen: params.seen === undefined ? undefined : String(params.seen),
    rule_id: params.ruleId === undefined ? undefined : String(params.ruleId),
    limit: params.limit === undefined ? undefined : String(params.limit),
  });
}

export function markAlertEventSeen(id: number, seen = true): Promise<AlertSeenResponse> {
  return apiSend<AlertSeenResponse>('POST', `/api/alert/events/${id}/seen`, { seen });
}

export type { AlertEvent };
