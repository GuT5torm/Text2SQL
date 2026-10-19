/**
 * 复杂查询表单接口封装（FR-P-1 ~ FR-P-5）。
 *
 * 与 /api/chat 是姊妹路径：同一个 state、同一张图、同一份落库，差别只在输入
 * 形态——指标、维度、条件由表单直接给出，route 不再让模型从问句里抽一遍。
 * 返回体与对话一致（ChatResult），所以结果区可以复用同一批展示组件。
 *
 * 条件字段与取值全部来自 /api/semantic/filters，不在前端另写一份：取值体系
 * 只有语义层一个来源，界面才不会给出库里没有的选项。
 */

import { apiGet, apiSend } from './client';
import type { ChatResult } from './types';

export type FormOp = 'eq' | 'in' | 'between' | 'gte' | 'lte';

/** 语义层登记的一个可用条件字段。 */
export interface FilterField {
  field: string;
  display: string;
  ops: FormOp[];
  values_from: 'enum' | 'date';
  join?: string;
  /** 双向登记的互斥字段。前端据此置灰，后端据此拒绝 */
  exclude_with?: string[];
  note?: string;
  /** 库内取值 → 中文标签；日期类字段为空 */
  values: { value: string; label: string }[];
}

export interface FormCondition {
  field: string;
  op: FormOp;
  value?: string | string[] | null;
  value_to?: string | null;
}

export interface FormQueryBody {
  metric: string;
  dimensions: string[];
  conditions: FormCondition[];
  time_range?: { start: string; end: string } | null;
  order_by?: { field: string; direction: 'asc' | 'desc' } | null;
  top_n?: number | null;
}

/** 预演结果：提交前给人看的可读问句与谓词形态。 */
export interface FormPreview {
  question: string;
  predicates: string[];
  descriptions: string[];
  conditions: unknown[];
}

let filtersPromise: Promise<FilterField[]> | null = null;

export function fetchFilterFields(): Promise<FilterField[]> {
  if (!filtersPromise) {
    filtersPromise = apiGet<{ items: FilterField[] }>('/api/semantic/filters').then(
      (res) => res.items,
    );
  }
  return filtersPromise;
}

export function previewFormQuery(body: FormQueryBody): Promise<FormPreview> {
  return apiSend<FormPreview>('POST', '/api/query/form/preview', body);
}

/** 提交表单查询。sessionId 由 POST /api/session 创建。 */
export function submitFormQuery(
  sessionId: string,
  body: FormQueryBody,
): Promise<ChatResult> {
  return apiSend<ChatResult>('POST', '/api/query/form', { session_id: sessionId, ...body });
}
