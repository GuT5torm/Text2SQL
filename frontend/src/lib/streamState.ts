/**
 * 流式请求终态与守卫告警的纯函数工具（FR-E-3 / FR-I-2）。
 *
 * 抽成无 React 依赖的纯函数，useChatStream 只负责打标，AnswerCard 只负责
 * 按标渲染——判定与展示分开，三态边界才有处可测。
 */

import type { GuardNotice } from '@/api/types';

/**
 * FR-E-3：请求结束却拿不到 result 时的三种终态。
 * - aborted：用户主动中断。不是故障，不给重试按钮（再点一次发送即可）。
 * - empty：流正常结束但没有 result 帧（代理超时 / 后端在结果前退出），值得重试。
 * - network：请求本身失败（断网、DNS、非 2xx、服务端 error 帧等），值得重试。
 */
export type ChatErrorKind = 'aborted' | 'empty' | 'network';

export function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError';
}

export const TERMINAL_COPY: Record<
  ChatErrorKind,
  { title: string; description: string; retryable: boolean; alertType: 'info' | 'warning' | 'error' }
> = {
  aborted: {
    title: '已中断本次请求',
    description: '请求已被手动中断，不会继续执行。需要结果时重新发送一次问题即可。',
    retryable: false,
    alertType: 'info',
  },
  empty: {
    title: '未收到服务端结果',
    description: '连接在返回结果前已关闭（可能是代理超时或服务重启），可重试本轮提问。',
    retryable: true,
    alertType: 'warning',
  },
  network: {
    title: '请求失败',
    description: '网络或服务异常，请检查连接后重试。',
    retryable: true,
    alertType: 'error',
  },
};

/**
 * 把后端守卫告警归一化成结构化双通道。
 *
 * 后端现行契约是 {kind, code, message}；历史记录 / 老版本接口里可能只剩
 * 纯字符串，按 rejected 通道兜底，code 留空。未知形态直接丢弃，不让一条
 * 脏数据搞垮整轮渲染。
 */
export function normalizeGuardNotices(raw: unknown): GuardNotice[] {
  if (!Array.isArray(raw)) return [];
  const out: GuardNotice[] = [];
  for (const item of raw) {
    if (typeof item === 'string') {
      if (item.trim()) out.push({ kind: 'rejected', code: '', message: item });
      continue;
    }
    if (item && typeof item === 'object') {
      const obj = item as Record<string, unknown>;
      const message = typeof obj.message === 'string' ? obj.message : '';
      if (!message) continue;
      const kind = obj.kind === 'rewritten' ? 'rewritten' : 'rejected';
      const code = typeof obj.code === 'string' ? obj.code : '';
      out.push({ kind, code, message });
    }
  }
  return out;
}
