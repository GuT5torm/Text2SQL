import { useCallback, useEffect, useRef, useState } from 'react';
import { streamChat } from '@/api/chat';
import type { ChatResult, SessionDetail, StageEvent, TurnItem } from '@/api/types';
import { isAbortError, TERMINAL_COPY, type ChatErrorKind } from '@/lib/streamState';

export interface ChatTurn {
  /** 前端本地生成的键。历史回看的轮次用服务端 turn id */
  key: string;
  /**
   * 落库后的轮次 ID（FR-G 反馈 / FR-N-3 分享接口的必填字段）。
   * SSE 流式过程未结束时为 null；onResult 收到 result 后从 result.turn_id 同步。
   */
  turnId: number | null;
  question: string;
  /**
   * FR-H-3：用户原话与改写问句。
   * 流式过程中两者都等于 question；result 帧到达后由 result.original_question /
   * rewritten_question 同步。历史回看从 TurnItem 同名字段取，缺字段时降级为 question。
   */
  originalQuestion?: string;
  rewrittenQuestion?: string;
  stages: StageEvent[];
  result: ChatResult | null;
  errorMessage: string | null;
  /**
   * FR-E-3：无 result 终态的三态标记。
   * aborted 不给重试；empty / network 给重试入口。有 result 时为 null。
   */
  errorKind: ChatErrorKind | null;
  streaming: boolean;
  createdAt: string | null;
}

/**
 * 历史轮次没有 stages（后端落库不存进度），其余字段按 ChatResult 的形状还原，
 * 让 AnswerCard 只认一种数据结构。
 */
export function turnToChatTurn(turn: TurnItem): ChatTurn {
  const result: ChatResult = {
    status: (turn.status as ChatResult['status']) ?? 'ok',
    answer: turn.answer ?? '',
    insight: '',
    highlights: [],
    turn_id: turn.turn_id ?? turn.id,
    sql: turn.sql ?? '',
    original_sql: '',
    sql_error: turn.error ?? '',
    guard_warnings: [],
    columns: turn.result?.columns ?? [],
    rows: turn.result?.rows ?? [],
    row_count: turn.row_count ?? turn.result?.row_count ?? 0,
    truncated: false,
    query_elapsed_ms: turn.elapsed_ms ?? 0,
    chart: turn.chart ?? null,
    metric_used: turn.metrics?.metric_used ?? [],
    caveats: turn.metrics?.caveats ?? [],
    time_range: turn.metrics?.time_range ?? null,
    retry_count: 0,
    intent: null,
    stages: [],
    // 历史回看没有 reasoning / source_tables / data_freshness 等运行时字段，
    // 留空字符串 / 空数组 / null，靠前端条件渲染降级（FR-H-2：不报错）。
    sql_reasoning: '',
    fix_reasoning: '',
    source_tables: [],
    data_freshness: null,
    persist_row_count: turn.result?.row_count ?? 0,
    node_path: [],
    // 对比基准与异常标记已随轮次落库（chat.py::_persist）。老库迁移的记录缺
    // 这两列，后端返回 null / []，前端按「这一轮没有基准」降级，不额外提示。
    baselines: turn.baselines ?? null,
    findings: turn.findings ?? [],
    partial_period: turn.partial_period ?? false,
    granularity: turn.granularity ?? null,
    // FR-I-2 / FR-B-3 / FR-D-5 / FR-I-3：历史轮次按同契约回放，缺字段降级
    guard_notices: turn.guard_notices ?? [],
    number_checks: turn.number_checks ?? [],
    time_range_consistent: turn.time_range_consistent ?? null,
    masked: turn.masked ?? false,
    masked_fields: turn.masked_fields ?? [],
    // FR-A-5：结构化槽位历史回看
    slots: turn.slots,
    // FR-H-3：原话与改写问句。老库迁移记录缺这两列时为 undefined，AnswerCard
    // 按 question 兜底显示，不报错。
    original_question: turn.original_question,
    rewritten_question: turn.rewritten_question,
    // FR-E-4：修正历史。老库迁移记录缺这列时降级为空数组（不展示该项）
    fix_history: turn.fix_history ?? [],
    // 失败归因：随轮次落库在 metrics_json，回看失败轮次仍能给出归因与修法
    failure_attribution: turn.metrics?.failure_attribution ?? null,
  };
  return {
    key: `turn-${turn.id}`,
    turnId: turn.turn_id ?? turn.id,
    question: turn.question,
    // 流式过程没有原话字段时按 question 兜底，保持卡片始终有可显示问句
    originalQuestion: turn.original_question ?? turn.question,
    rewrittenQuestion: turn.rewritten_question ?? '',
    stages: [],
    result,
    errorMessage: null,
    errorKind: null,
    streaming: false,
    createdAt: turn.created_at ?? null,
  };
}

export function useChatStream() {
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [streaming, setStreaming] = useState(false);
  // 每轮一个 controller。单槽位时两个并发请求会互相覆盖：先结束的那个会抹掉
  // 后一个的 controller 并提前解锁输入框，后一个请求从此无法中断。
  const abortRef = useRef<Map<string, AbortController>>(new Map());
  const mountedRef = useRef(true);
  const seqRef = useRef(0);

  // 卸载时中断在途请求：切到历史页后旧请求仍在跑，算力白耗，
  // 且本轮结果已经没有接收方了
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      abortRef.current.forEach((controller) => controller.abort());
      abortRef.current.clear();
    };
  }, []);

  const patch = useCallback((key: string, update: (turn: ChatTurn) => Partial<ChatTurn>) => {
    if (!mountedRef.current) return;
    setTurns((prev) => prev.map((t) => (t.key === key ? { ...t, ...update(t) } : t)));
  }, []);

  const abortAll = useCallback(() => {
    abortRef.current.forEach((controller) => controller.abort());
    abortRef.current.clear();
  }, []);

  const send = useCallback(
    async (sessionId: string, question: string) => {
      const key = `local-${++seqRef.current}`;
      setTurns((prev) => [
        ...prev,
        { key, question, turnId: null, stages: [], result: null, errorMessage: null, errorKind: null, streaming: true, createdAt: null },
      ]);
      setStreaming(true);

      const controller = new AbortController();
      abortRef.current.set(key, controller);

      try {
        await streamChat(
          sessionId,
          question,
          {
            onStage: (stage) => patch(key, (t) => ({ stages: [...t.stages, stage] })),
            onResult: (result) =>
              patch(key, () => ({
                result,
                stages: result.stages,
                streaming: false,
                // result 落库后由 _persist 回填 turn_id；未回填（落库失败）时保持 null
                turnId: result.turn_id ?? null,
                // FR-H-3：route 节点判定需要补全时，原话与改写问句都不同。
                // result 帧到达前卡片 question 仍是用户输入的原文；到达后用
                // 服务端的两个字段覆盖，让「问句来源」面板能展示真实差异。
                originalQuestion: result.original_question ?? question,
                rewrittenQuestion: result.rewritten_question ?? '',
              })),
            onError: (message) =>
              patch(key, () => ({ errorMessage: message, errorKind: 'network', streaming: false })),
            onDone: () =>
              patch(key, (t) => {
                // 流结束却既没有结果也没有报错，说明连接是在 result 帧之前断掉的
                // （反向代理超时、后端进程退出）。不写文案的话这一轮永远停在
                // 骨架屏：streaming 已复位，用户既看不到错误也没有重试入口。
                if (t.result || t.errorMessage) return { streaming: false };
                return {
                  streaming: false,
                  errorKind: 'empty',
                  errorMessage: TERMINAL_COPY.empty.description,
                };
              }),
          },
          controller.signal,
        );
      } catch (error: unknown) {
        if (isAbortError(error)) {
          // 用户主动中断不是错误，但也要有终态文案，否则卡片会空着。
          // FR-E-3：aborted 态不给重试按钮——重新发送问题本身就是重试。
          patch(key, (t) =>
            t.result || t.errorMessage
              ? { streaming: false }
              : {
                  streaming: false,
                  errorKind: 'aborted',
                  errorMessage: TERMINAL_COPY.aborted.description,
                },
          );
          return;
        }
        const message = error instanceof Error ? error.message : String(error);
        patch(key, () => ({ errorMessage: message, errorKind: 'network', streaming: false }));
      } finally {
        // 槽位仍是本次的 controller 才清理：晚结束的请求不能删掉新一轮的
        if (abortRef.current.get(key) === controller) abortRef.current.delete(key);
        // 还有别的请求在跑就不能解锁输入框
        if (mountedRef.current && abortRef.current.size === 0) setStreaming(false);
      }
    },
    [patch],
  );

  const abort = useCallback(() => abortAll(), [abortAll]);

  const reset = useCallback(() => {
    // 新建会话前先中断在途请求，否则旧请求的结果会落进新会话的列表里
    abortAll();
    setTurns([]);
    setStreaming(false);
  }, [abortAll]);

  const loadHistory = useCallback(
    (detail: SessionDetail) => {
      abortAll();
      setTurns(detail.turns.map(turnToChatTurn));
      setStreaming(false);
    },
    [abortAll],
  );

  return { turns, streaming, send, abort, reset, loadHistory };
}
