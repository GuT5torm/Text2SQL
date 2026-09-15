import type { ChatResult, StageEvent, StreamEvent } from './types';
import { ApiError } from './client';

/**
 * SSE 接收。用 fetch 而不是 EventSource：EventSource 只支持 GET，
 * 而提问内容要走 POST 请求体（问题最长 500 字，放进查询串不可靠）。
 */

export interface StreamHandlers {
  onStage: (stage: StageEvent) => void;
  onResult: (result: ChatResult) => void;
  onError: (message: string) => void;
  onDone: () => void;
}

/** 按 SSE 帧边界切分。帧与帧之间是空行，每帧内是 `字段: 值` 行。 */
export function parseSseChunk(raw: string): { frames: string[]; rest: string } {
  // 后端固定输出 LF，但响应若经过一层会重写行尾的代理（或 Windows 上的
  // 中间层），帧边界会变成 CRLF。只按 \n\n 切会把整条流当成一个残帧。
  const parts = raw.replace(/\r\n/g, '\n').split('\n\n');
  const rest = parts.pop() ?? '';
  return { frames: parts, rest };
}

export function parseFrame(frame: string): StreamEvent | null {
  let event = 'message';
  const dataLines: string[] = [];

  for (const line of frame.split('\n')) {
    if (!line || line.startsWith(':')) continue;
    const idx = line.indexOf(':');
    const field = idx === -1 ? line : line.slice(0, idx);
    const value = idx === -1 ? '' : line.slice(idx + 1).replace(/^ /, '');
    if (field === 'event') event = value;
    else if (field === 'data') dataLines.push(value);
  }

  const payload = dataLines.join('\n');
  if (event === 'done') return { type: 'done' };

  let data: unknown;
  try {
    data = payload ? JSON.parse(payload) : null;
  } catch {
    // result 帧解析不出来意味着这一轮拿不到结果，必须让调用方知道；
    // stage 帧只用于进度展示，丢一条不影响最终结果，静默跳过即可
    return event === 'result'
      ? { type: 'invalid', data: { message: '服务端返回的结果无法解析，请重试' } }
      : null;
  }

  if (event === 'stage') return { type: 'stage', data: data as StageEvent };
  if (event === 'result') return { type: 'result', data: data as ChatResult };
  if (event === 'error') return { type: 'error', data: data as { message: string } };
  return null;
}

export async function streamChat(
  sessionId: string,
  question: string,
  handlers: StreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch('/api/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    body: JSON.stringify({ session_id: sessionId, question }),
    signal,
  });

  if (!response.ok || !response.body) {
    throw new ApiError(response.status, await response.text().catch(() => response.statusText));
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder('utf-8');
  let buffer = '';

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      const { frames, rest } = parseSseChunk(buffer);
      buffer = rest;
      for (const frame of frames) {
        const event = parseFrame(frame);
        if (!event) continue;
        if (event.type === 'stage') handlers.onStage(event.data);
        else if (event.type === 'result') handlers.onResult(event.data);
        else if (event.type === 'invalid') handlers.onError(event.data.message);
        else if (event.type === 'error') handlers.onError(event.data?.message ?? '服务端返回了错误');
      }
    }

    // 流结束时缓冲区里可能还留着最后一帧：后端若未以空行收尾（或被中间层
    // 改写），这一帧会被永久丢弃，前端就永远等不到 result
    const tail = buffer.trim();
    if (tail) {
      const event = parseFrame(tail);
      if (event?.type === 'result') handlers.onResult(event.data);
      else if (event?.type === 'invalid') handlers.onError(event.data.message);
      else if (event?.type === 'error') handlers.onError(event.data?.message ?? '服务端返回了错误');
    }
  } finally {
    reader.cancel().catch(() => undefined);
    handlers.onDone();
  }
}
