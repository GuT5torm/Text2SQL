import { useCallback, useEffect, useRef, useState } from 'react';
import { streamChat } from '@/api/chat';
import type { ChatResult, SessionDetail, StageEvent, TurnItem } from '@/api/types';

export interface ChatTurn {
  /** 前端本地生成的键。历史回看的轮次用服务端 turn id */
  key: string;
  question: string;
  stages: StageEvent[];
  result: ChatResult | null;
  errorMessage: string | null;
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
  };
  return {
    key: `turn-${turn.id}`,
    question: turn.question,
    stages: [],
    result,
    errorMessage: null,
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
        { key, question, stages: [], result: null, errorMessage: null, streaming: true, createdAt: null },
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
              patch(key, () => ({ result, stages: result.stages, streaming: false })),
            onError: (message) => patch(key, () => ({ errorMessage: message, streaming: false })),
            onDone: () =>
              patch(key, (t) => {
                // 流结束却既没有结果也没有报错，说明连接是在 result 帧之前断掉的
                // （反向代理超时、后端进程退出）。不写文案的话这一轮永远停在
                // 骨架屏：streaming 已复位，用户既看不到错误也没有重试入口。
                if (t.result || t.errorMessage) return { streaming: false };
                return {
                  streaming: false,
                  errorMessage: '未收到服务端结果，连接可能已中断，请重试',
                };
              }),
          },
          controller.signal,
        );
      } catch (error: unknown) {
        if (error instanceof DOMException && error.name === 'AbortError') {
          // 用户主动中断不是错误，但也要有终态文案，否则卡片会空着
          patch(key, (t) =>
            t.result || t.errorMessage
              ? { streaming: false }
              : { streaming: false, errorMessage: '已中断本次请求' },
          );
          return;
        }
        const message = error instanceof Error ? error.message : String(error);
        patch(key, () => ({ errorMessage: message, streaming: false }));
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
