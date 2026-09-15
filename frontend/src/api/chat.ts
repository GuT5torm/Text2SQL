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
  const parts = raw.split('\n\n');
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
    return null;
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
        else if (event.type === 'error') handlers.onError(event.data?.message ?? '服务端返回了错误');
      }
    }
  } finally {
    reader.cancel().catch(() => undefined);
    handlers.onDone();
  }
}
