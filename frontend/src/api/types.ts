/**
 * 前端只做展示与字段校验，不参与业务判断。
 * 这里的类型与后端 app/agent/graph.py::summarize_result、app/api/session.py 的输出逐字段对齐，
 * 字段名一律沿用后端（snake_case），不做改名，避免两层命名不一致带来的理解成本。
 */

/**
 * 意图分类。由 route 节点判定，问答失败时依然会带回。
 * out_of_scope=超出数据集能力范围；attribution=问原因与因果。两者都不生成 SQL。
 */
export type Intent = 'query' | 'chitchat' | 'clarify' | 'out_of_scope' | 'attribution' | null;

/** 一轮问答的终态。status 决定前端渲染哪一类结果分支。 */
export type ResultStatus =
  | 'ok'
  | 'clarify'
  | 'chitchat'
  | 'out_of_scope'
  | 'attribution'
  | 'error';

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

/**
 * 数据时效（FR-D-2）。后端按 orders / refunds 两表分别取 MAX(created_at)，
 * 与 DATA_CUTOFF_OVERRIDE 配置项的强制覆盖做混合源。
 */
export interface DataFreshness {
  orders_cutoff?: string | null;
  refunds_cutoff?: string | null;
  orders_age_days?: number | null;
  refunds_age_days?: number | null;
  is_stale?: boolean;
  threshold_days?: number;
  source?: string;
  note?: string;
}

/** 对比基准的一个窗口（FR-M-1）。deviation 为空表示不可比，前端不得拼成结论句。 */
export interface BaselineWindow {
  start: string;
  end: string;
  label: string;
  value: number | null;
  /** 相对偏离。null 表示不可比（不完整周期或基数为零），不是 0 */
  deviation: number | null;
  /** 该偏离度能否用于结论。false 时值仍可用于标注 */
  comparable?: boolean;
}

/**
 * 对比基准（FR-M-1 至 FR-M-3）。
 * note 为「无对比期数据」时表示本轮没有基准，具体原因在 missing_reasons。
 */
export interface Baselines {
  metric?: string | null;
  granularity?: string | null;
  current_value?: number | null;
  current_period?: { start?: string; end?: string; label?: string } | null;
  mom?: BaselineWindow | null;
  yoy?: BaselineWindow | null;
  partial_period?: boolean;
  note?: string;
  missing_reasons?: string[];
  /** 命中的基准规则名（来自 baseline_rules.yaml），如 default / 客单价 / 退款率 */
  rule?: string;
}

/** 异常标记的严重度。两级，与 semantic/finding_rules.yaml 的 severity 取值一致。 */
export type FindingSeverity = '提示' | '关注';

/** 异常标记的产生规则。业务阈值优先于统计排名，去重时据此取舍。 */
export type FindingRule = 'deviation' | 'rank' | 'absolute';

/** 异常标记（FR-L-1）。判定全在服务端，前端只负责呈现与发起下钻。 */
export interface Finding {
  metric: string;
  /** 维度取自结果集列名，可能是口语简称（如「大区」） */
  dimension: string | null;
  slice: unknown;
  value: number;
  baseline: number | null;
  deviation: number | null;
  severity: FindingSeverity;
  rule: FindingRule;
  reason: string;
}

/** 下钻请求（FR-L-2）。字段名与 Finding 同名，标记可原样提交，不需要映射。 */
export interface DrilldownRequest {
  source: 'finding' | 'cell';
  metric?: string | null;
  dimension?: string | null;
  slice?: string | null;
  time_label?: string | null;
}

export interface DrilldownResponse {
  question: string;
  available: boolean;
  source: string;
  metric: string | null;
  dimension: string | null;
  slice: string | null;
  next_dimension: string | null;
  next_dimension_display: string | null;
  reason: string;
}

/**
 * 守卫告警双通道（FR-I-2）。
 * rejected：语句被沙箱拒绝（带 R1–R9 编号，执行层错误编号为空）；
 * rewritten：语句被改写后放行（如 R6 自动补行数上限），仅提示。
 */
export interface GuardNotice {
  kind: 'rejected' | 'rewritten';
  /** 规则编号 R1–R9；执行层错误 / 历史老数据没有编号时为空串 */
  code: string;
  message: string;
}

/** FR-B-3：结论中未能从结果集回溯的数字。 */
export interface NumberCheck {
  /** 结论里出现的原始数字文本，如「1,234」 */
  text: string;
  /** 解析出的数值；无法解析为数字时为 null */
  value: number | null;
}

/**
 * FR-A-5：结构化槽位。把问题拆成 {指标, 维度, 过滤, 时间, 排序, TopN} 六类。
 * route 节点产出，供前端标注与评测断言。
 */
export interface Slots {
  metrics: string[];
  dimensions: string[];
  filters: Array<{ field: string; value: string; op?: string }>;
  time_range: string;
  order_by: { field: string; direction: string } | null;
  top_n: number | null;
}

/**
 * 槽位覆盖核对的一处缺失。kind 取值与后端 slot_coverage.GAP_KINDS 一致：
 * dimension / filter / time_range / top_n / order_by。
 */
export interface SlotGap {
  kind: string;
  /** 槽位要求，如「限定「区域」为 华东」 */
  expected: string;
  /** 语句实际是什么样 */
  detail: string;
}

/** FR-C-2：SQL 解释的分段说明 */
export interface SqlExplainSegment {
  index: number;
  text: string;
}

/** FR-C-2：SQL 解释接口返回体 */
export interface SqlExplainResponse {
  segments: SqlExplainSegment[];
  tables: string[];
  error?: string;
}

/**
 * FR-E-4 修正历史条目。fix 节点每次修正追加一条，结构 [{stage, error}]。
 * stage 取自当时出错节点的名字（execute / validate），error 截断到 300 字。
 */
export interface FixHistoryItem {
  stage: string;
  error: string;
}

/**
 * 失败归因（与 eval 报表同一套编码）。
 *
 * 四类对应四种修法，界面上同时给出标签与建议：只给标签等于让用户自己去猜
 * 该改什么。成功轮为 null。
 */
export interface FailureAttribution {
  /** guard 守卫拦下 / signature 口径签名不符 / slot 条件没落进语句 / generate 生成侧未收敛 */
  kind: 'guard' | 'signature' | 'slot' | 'generate';
  label: string;
  suggestion: string;
  /** 判定依据：守卫规则编号、口径报错原文或「无更具体的失败证据」 */
  evidence: string;
}

/** SSE 的 result 帧，也是 /api/chat/sync 的返回体。 */
export interface ChatResult {
  status: ResultStatus;
  answer: string;
  insight: string;
  highlights: string[];
  /**
   * 落库后回填的轮次 ID。前端据此调用 feedback / share 接口。
   * SSE 流式过程中 result 帧到达前为 null；chat/sync 总是已落库。
   */
  turn_id: number | null;
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
  /** 写法说明（FR-C-1），与 generate 节点 reasoning 同源 */
  sql_reasoning: string;
  /** 写法说明（FR-F-2），仅在发生过 fix 重试时非空 */
  fix_reasoning: string;
  /** 实际访问的来源表（FR-D-1），可能为空 */
  source_tables: string[];
  /** 数据时效（FR-D-2），对象含 orders_cutoff / refunds_cutoff / is_stale / threshold_days / note */
  data_freshness: DataFreshness | null;
  /** 落库的行数。区分执行端截断与落库端截断（FR-H-2） */
  persist_row_count: number;
  /** 经过的节点序列（节点名去重，fix 回环取首次） */
  node_path: string[];
  /** 对比基准（FR-M-1）。null 表示本轮没算出基准 */
  baselines: Baselines | null;
  /** 本期是否不完整（FR-M-2）。为 true 时偏离度不可用于结论 */
  partial_period: boolean;
  /** 基准粒度：day / week / month / year，null 表示未限定时间范围 */
  granularity: string | null;
  /** 异常标记（FR-L-1） */
  findings: Finding[];
  /**
   * 结构化守卫告警（FR-I-2）。老版本接口可能只给字符串或缺失，
   * 渲染前统一过 normalizeGuardNotices。
   */
  guard_notices?: Array<GuardNotice | string>;
  /** 结论中未能从结果集回溯的数字（FR-B-3），空数组表示全部可回溯 */
  number_checks?: NumberCheck[];
  /**
   * 实际 SQL 时间谓词与召回时间区间是否一致（FR-D-5）。
   * null 表示未核对（SQL 无召回 / 无时间谓词需求 / 核对器异常），不是 false。
   */
  time_range_consistent?: boolean | null;
  /** 本轮结果是否触发过敏感字段脱敏（FR-I-3） */
  masked?: boolean;
  /** 被脱敏的字段标签（表.字段 物理名或别名投影） */
  masked_fields?: string[];
  /** FR-A-5：结构化槽位 {指标, 维度, 过滤, 时间, 排序, TopN} */
  slots?: Slots;
  /**
   * 槽位覆盖核对：问题里问到了、但语句没有落实的部分。
   * 空数组表示全部落实。它不阻断执行，只是把「语句合法但答的不是问的那件事」
   * 暴露出来供用户核对。
   */
  slot_gaps?: SlotGap[];
  /**
   * FR-H-3：用户原话。route 节点判定需要补全时，原话仍留在此字段，
   * 改写后的问句走 question（ChatResult.answer 与 turn.question 已用之）。
   * 未改写时与 question 同值；老库迁移记录缺这列时为空字符串。
   */
  original_question?: string;
  /** FR-H-3：路由改写后的问句。未改写时为空串，前端按「未改写」降级。 */
  rewritten_question?: string;
  /** FR-E-4：修正历史。空数组表示未发生过修正（fix 节点未触发） */
  fix_history?: FixHistoryItem[];
  /** 失败归因。仅 status 为 error 时非空，成功轮为 null */
  failure_attribution?: FailureAttribution | null;
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
  /** 与 id 同值，前端按此字段调用 feedback / share 接口 */
  turn_id?: number;
  question: string;
  answer: string;
  sql: string | null;
  metrics:
    | {
        metric_used?: string[];
        caveats?: string[];
        time_range?: TimeRange | null;
        /** 失败归因随轮次落库（与 slot_gaps 同路），回看失败轮次时可用 */
        failure_attribution?: FailureAttribution | null;
      }
    | null;
  chart: ChartPayload | null;
  result: { columns?: string[]; rows?: Record<string, unknown>[]; row_count?: number } | null;
  row_count: number;
  elapsed_ms: number;
  status: ResultStatus | string;
  error: string | null;
  created_at: string | null;
  /** 对比基准与异常标记。老库迁移的记录缺这两列，前端按缺失降级 */
  baselines?: Baselines | null;
  findings?: Finding[];
  partial_period?: boolean;
  granularity?: string | null;
  /** FR-I-2 / FR-B-3 / FR-D-5 / FR-I-3：与 ChatResult 同契约的历史回放字段 */
  guard_notices?: Array<GuardNotice | string>;
  number_checks?: NumberCheck[];
  time_range_consistent?: boolean | null;
  masked?: boolean;
  masked_fields?: string[];
  /** FR-A-5：结构化槽位历史回看 */
  slots?: Slots;
  /** 槽位覆盖核对结果，与 ChatResult 同契约 */
  slot_gaps?: SlotGap[];
  /** FR-H-3：用户原话与改写问句。老库迁移记录缺这两列时按 undefined 降级 */
  original_question?: string;
  rewritten_question?: string;
  /** FR-E-4：修正历史。空数组表示未发生过修正 */
  fix_history?: FixHistoryItem[];
  /** 失败归因。老库迁移记录缺此字段时按 undefined 降级，不渲染归因条 */
  failure_attribution?: FailureAttribution | null;
}

export interface SessionDetail extends SessionItem {
  turns: TurnItem[];
}

export interface HealthInfo {
  status: string;
  llm_mock: boolean;
  model: string;
  /** 实际生效的 checkpointer 模式（memory / sqlite，FR-J-3） */
  checkpointer: string;
  metrics: number;
  tables: number;
  /** 业务库数据截至时刻（orders / refunds cutoff 取早者） */
  data_as_of?: string | null;
  data_freshness?: DataFreshness | null;
  /** 启用中的告警规则数 */
  alert_rules?: number;
  alert_last_eval_at?: string | null;
  alert_consecutive_failures?: number;
  alert_failures_threshold?: number;
  alert_unhealthy?: boolean;
  alert_unread?: number;
}

// —— FR-O-1 至 FR-O-5 异常订阅 ——

/** 比较方式：高于阈值 / 低于阈值 / 偏离度（deviation 仅支持整体口径） */
export type AlertComparison = 'gt' | 'lt' | 'deviation';

/** 严重度：提示 / 关注 */
export type AlertSeverity = 'notice' | 'attention';

export interface AlertRule {
  id: number;
  metric: string;
  /** 维度物理名（或显示名，后端两者都认）；空数组表示整体口径 */
  dimensions: string[];
  comparison: AlertComparison;
  /** gt/lt 为指标阈值，deviation 为百分比数值（5 表示 5%） */
  threshold: number;
  severity: AlertSeverity;
  enabled: boolean;
  created_at?: string | null;
  updated_at?: string | null;
}

/** upsert 请求体：id 缺省为新建，带 id 为更新 */
export type AlertRuleBody = Omit<AlertRule, 'id' | 'created_at' | 'updated_at'> & {
  id?: number | null;
};

/** 命中切片。deviation 订阅时 baseline 携带 {label, deviation}，其余为 null */
export interface AlertHit {
  /** 维度显示名 → 切片值；整体口径为 null */
  slice: Record<string, unknown> | null;
  value: number | null;
  baseline: { label?: string; deviation?: number } | null;
  reason: string;
  /** 一键下钻问句，直接回填首页提问框 */
  question: string;
}

/** 一次求值的事件记录。(rule_id, evaluated_at) 幂等，同周期复检返回同一条 */
export interface AlertEvent {
  id: number;
  rule_id: number;
  evaluated_at: string;
  /** ok：求值成功（含未命中）；error：链路失败 / 口径不成立 */
  status: 'ok' | 'error' | string;
  hit: boolean;
  metric: string | null;
  hits: AlertHit[];
  value: number | null;
  reason: string;
  seen: boolean;
  created_at?: string | null;
  /** 来自关联规则，规则被删后为 null */
  severity: AlertSeverity | null;
}

export interface AlertRulesResponse {
  rules: AlertRule[];
}

export interface AlertEventsResponse {
  events: AlertEvent[];
  unread: number;
}

export interface AlertCheckResponse {
  evaluated_at: string;
  checked: number;
  events: AlertEvent[];
}

export interface AlertSeenResponse {
  id: number;
  seen: boolean;
  unread: number;
}

// —— FR-G-1 反馈闭环 ——
export type FeedbackRating = 'up' | 'down';

/**
 * 点踩原因（FR-G-2）。reason_code 与后端枚举对齐：
 * wrong_metric / wrong_field / wrong_number / off_topic / other
 */
export type FeedbackReasonCode =
  | 'wrong_metric'
  | 'wrong_field'
  | 'wrong_number'
  | 'off_topic'
  | 'other'
  | '';

export interface FeedbackItem {
  id: number;
  turn_id: number;
  session_id: string;
  rating: FeedbackRating;
  reason_code: FeedbackReasonCode;
  note: string;
  created_at: string;
  updated_at: string;
}

export interface FeedbackSummary {
  total: number;
  up: number;
  down: number;
  reason_counts: Record<string, number>;
  items: FeedbackItem[];
}

export interface FeedbackExport {
  count: number;
  yaml: string;
  note: string;
}

export interface FeedbackSubmitResponse {
  id: number;
  feedback: FeedbackItem;
  written: boolean;
}

// —— FR-N-3 只读分享 ——
export interface ShareRequest {
  turn_id: number;
  session_id: string;
  expires_in_days?: number | null;
}

export interface ShareCreateResponse {
  token: string;
  turn_id: number;
  session_id: string;
  expires_at: string | null;
  created_at: string;
}

/**
 * 分享页快照。结构与 ChatResult 子集对齐，但 stages / sql_reasoning 等
 * 运行时字段缺失——分享页按历史回看同款渲染，缺字段降级处理。
 */
export interface ShareSnapshot {
  question: string;
  answer: string;
  sql: string;
  status: string;
  error: string;
  metric_used: string[];
  caveats: string[];
  time_range: TimeRange | null;
  chart: ChartPayload | null;
  result: { columns: string[]; rows: Record<string, unknown>[]; row_count: number };
  row_count: number;
  elapsed_ms: number;
  baselines: Baselines | null;
  findings: Finding[];
  partial_period: boolean;
  granularity: string | null;
  source_tables: string[];
  created_at: string | null;
  /** FR-I-2 / FR-B-3 / FR-D-5 / FR-I-3：分享快照同契约字段，老快照缺失时降级 */
  guard_notices?: Array<GuardNotice | string>;
  number_checks?: NumberCheck[];
  time_range_consistent?: boolean | null;
  masked?: boolean;
  masked_fields?: string[];
}

export interface ShareReadResponse {
  token: string;
  turn_id: number | null;
  session_id: string | null;
  snapshot: ShareSnapshot;
  expires_at: string | null;
  created_at: string | null;
}

/** SSE 事件类型。done 为后端主动下发的终止帧，invalid 为解析不出内容的坏帧。 */
export type StreamEvent =
  | { type: 'stage'; data: StageEvent }
  | { type: 'result'; data: ChatResult }
  | { type: 'error'; data: { message: string } }
  | { type: 'invalid'; data: { message: string } }
  | { type: 'done' };
