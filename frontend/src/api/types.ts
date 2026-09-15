/**
 * 前端只做展示与字段校验，不参与业务判断。
 * 这里的类型与后端 app/agent/graph.py::summarize_result、app/api/session.py 的输出逐字段对齐，
 * 字段名一律沿用后端（snake_case），不做改名，避免两层命名不一致带来的理解成本。
 */

/** 意图分类。由 route 节点判定，问答失败时依然会带回。 */
export type Intent = 'query' | 'chitchat' | 'clarify' | null;

/** 一轮问答的终态。status 决定前端渲染哪一类结果分支。 */
export type ResultStatus = 'ok' | 'clarify' | 'chitchat' | 'error';

/** 节点进度事件。SSE 的 stage 帧即为该结构，按到达顺序追加、不重排。 */
export interface StageEvent {
  node: string;
  label: string;
  detail: string;
  elapsed_ms: number;
}

/**
 * 图表载荷。chart_type 由后端决定，前端不再重新决策业务图型；
 * option 为 ECharts 配置，为 null 时前端退回表格。
 */
export interface ChartPayload {
  chart_type: string;
  option: Record<string, unknown> | null;
  reason: string;
  warnings: string[];
  table_columns: string[];
  table_rows: Record<string, unknown>[];
}

export interface TimeRange {
  label?: string;
  /** 半开区间 [start, end)，SQL 里按 start <= t < end 比较 */
  start?: string;
  end?: string;
  /** 换算后的自然语言描述，如「2026-08-01 起至 2026-08-31 止」 */
  resolved?: string;
}

/** SSE 的 result 帧，也是 /api/chat/sync 的返回体。 */
export interface ChatResult {
  status: ResultStatus;
  answer: string;
  insight: string;
  highlights: string[];
  /** 实际执行的语句，可能已被行数上限包裹改写 */
  sql: string;
  /** 模型生成的原始语句，人工核对时用这一条 */
  original_sql: string;
  sql_error: string;
  guard_warnings: string[];
  columns: string[];
  rows: Record<string, unknown>[];
  /** 本轮下发的行数。truncated 为 true 时不等于真实命中行数 */
  row_count: number;
  truncated: boolean;
  query_elapsed_ms: number;
  chart: ChartPayload | null;
  metric_used: string[];
  caveats: string[];
  time_range: TimeRange | null;
  retry_count: number;
  intent: Intent;
  stages: StageEvent[];
}

export interface SessionItem {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  archived: boolean;
  turn_count: number;
}

export interface TurnItem {
  id: number;
  question: string;
  answer: string;
  sql: string | null;
  metrics: { metric_used?: string[]; caveats?: string[]; time_range?: TimeRange | null } | null;
  chart: ChartPayload | null;
  result: { columns?: string[]; rows?: Record<string, unknown>[]; row_count?: number } | null;
  row_count: number;
  elapsed_ms: number;
  status: ResultStatus | string;
  error: string | null;
  created_at: string | null;
}

export interface SessionDetail extends SessionItem {
  turns: TurnItem[];
}

export interface HealthInfo {
  status: string;
  llm_mock: boolean;
  model: string;
  checkpointer: string;
  metrics: number;
  tables: number;
}

/** SSE 事件类型。done 为后端主动下发的终止帧，invalid 为解析不出内容的坏帧。 */
export type StreamEvent =
  | { type: 'stage'; data: StageEvent }
  | { type: 'result'; data: ChatResult }
  | { type: 'error'; data: { message: string } }
  | { type: 'invalid'; data: { message: string } }
  | { type: 'done' };
