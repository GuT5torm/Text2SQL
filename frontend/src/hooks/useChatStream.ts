import { useCallback, useRef, useState } from 'react';
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
  const abortRef = useRef<AbortController | null>(null);
  const seqRef = useRef(0);

  const send = useCallback(async (sessionId: string, question: string) => {
    const key = `local-${++seqRef.current}`;
    setTurns((prev) => [
      ...prev,
      { key, question, stages: [], result: null, errorMessage: null, streaming: true, createdAt: null },
    ]);
    setStreaming(true);

    const controller = new AbortController();
    abortRef.current = controller;

    await streamChat(
      sessionId,
      question,
      {
        onStage: (stage) =>
          setTurns((prev) =>
            prev.map((t) => (t.key === key ? { ...t, stages: [...t.stages, stage] } : t)),
          ),
        onResult: (result) =>
          setTurns((prev) =>
            prev.map((t) => (t.key === key ? { ...t, result, stages: result.stages, streaming: false } : t)),
          ),
        onError: (message) =>
          setTurns((prev) =>
            prev.map((t) => (t.key === key ? { ...t, errorMessage: message, streaming: false } : t)),
          ),
        onDone: () =>
          setTurns((prev) => prev.map((t) => (t.key === key ? { ...t, streaming: false } : t))),
      },
      controller.signal,
    ).catch((error: unknown) => {
      // AbortController 取消是用户主动中断，不当成错误展示
      if (error instanceof DOMException && error.name === 'AbortError') {
        setTurns((prev) => prev.map((t) => (t.key === key ? { ...t, streaming: false } : t)));
        return;
      }
      const message = error instanceof Error ? error.message : String(error);
      setTurns((prev) =>
        prev.map((t) => (t.key === key ? { ...t, errorMessage: message, streaming: false } : t)),
      );
    });

    setStreaming(false);
    abortRef.current = null;
  }, []);

  const abort = useCallback(() => abortRef.current?.abort(), []);

  const reset = useCallback(() => {
    setTurns([]);
    setStreaming(false);
  }, []);

  const loadHistory = useCallback((detail: SessionDetail) => {
    setTurns(detail.turns.map(turnToChatTurn));
    setStreaming(false);
  }, []);

  return { turns, streaming, send, abort, reset, loadHistory };
}
