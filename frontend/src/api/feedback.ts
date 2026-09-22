/**
 * 反馈闭环 API（FR-G-1 至 FR-G-4）。
 *
 * 一条 turn 至多一条 feedback：重复提交由后端按更新处理（UNIQUE turn_id）。
 * 前端不缓存反馈状态——历史回看时由 AnswerCard 主动拉一次 getFeedbackByTurn
 * 把已有的反馈回填进 FeedbackBar，避免用户改了原因后看不到当前状态。
 */

import { apiGet, apiSend } from './client';
import type {
  FeedbackExport,
  FeedbackItem,
  FeedbackRating,
  FeedbackReasonCode,
  FeedbackSubmitResponse,
  FeedbackSummary,
} from './types';

export function submitFeedback(payload: {
  turn_id: number;
  session_id: string;
  rating: FeedbackRating;
  reason_code?: FeedbackReasonCode;
  note?: string;
}) {
  return apiSend<FeedbackSubmitResponse>('POST', '/api/feedback', {
    turn_id: payload.turn_id,
    session_id: payload.session_id,
    rating: payload.rating,
    reason_code: payload.reason_code ?? '',
    note: payload.note ?? '',
  });
}

export async function getFeedbackByTurn(turnId: number): Promise<FeedbackItem | null> {
  // 没有「按 turn 查」的专门接口，summary 接口拉回所有反馈再在前端按 turn_id 筛。
  // 调用方期望返回 null 而不是抛异常——后端 404 也视作「没有反馈」。
  let summary: FeedbackSummary | null;
  try {
    summary = await apiGet<FeedbackSummary>('/api/feedback/summary');
  } catch {
    return null;
  }
  if (!summary) return null;
  const items = summary.items ?? [];
  return items.find((it) => it.turn_id === turnId) ?? null;
}

export function getFeedbackSummary(sessionId?: string, rating?: FeedbackRating) {
  return apiGet<FeedbackSummary>('/api/feedback/summary', {
    session_id: sessionId,
    rating: rating ?? undefined,
  });
}

export function exportFeedback(sessionId?: string) {
  return apiGet<FeedbackExport>('/api/feedback/export', {
    session_id: sessionId,
  });
}
