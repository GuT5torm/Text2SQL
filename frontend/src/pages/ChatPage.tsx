import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { Alert, Button, Space, Typography, message } from 'antd';
import { ChatPanel } from '@/components/ChatPanel';
import { useChatStream } from '@/hooks/useChatStream';
import { requestDrilldown } from '@/api/drilldown';
import { createSession, getSession } from '@/api/session';
import type { DrilldownRequest, SessionItem } from '@/api/types';

const { Text } = Typography;

/**
 * 会话是否被换掉由 session_id 决定：换 id 就 reload 历史，
 * 同一个会话内的多轮由 useChatStream 自己累积，不重复拉取。
 *
 * 布局：单栏对话流。每轮回答（含图表 / 明细 / SQL）直接内联在问题之下，
 * 不再依赖原先的「右侧结果区 + 点击聚焦」——那样问答不同框，多轮时一次
 * 只能看一轮结果。会话的切换 / 新建 / 重命名统一交给左侧常驻侧边栏。
 */
export function ChatPage() {
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const sessionId = params.get('session');

  const [session, setSession] = useState<SessionItem | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const { turns, streaming, send, abort, loadHistory } = useChatStream();
  const loadedRef = useRef<string | null>(null);
  // FR-O-4：告警中心一键下钻带 ?q= 跳来，同一会话同一问句只自动发送一次
  const consumedQuestionRef = useRef<string | null>(null);

  // 没有 session 参数时先建一个，避免用户第一句话发出去才发现无处可发
  useEffect(() => {
    if (sessionId) return;
    let cancelled = false;
    createSession()
      .then((rec) => {
        if (cancelled) return;
        // 保留 ?q=：新建会话 replace 掉查询串会把告警下钻问句一起抹掉
        const next = new URLSearchParams(params);
        next.set('session', rec.id);
        setParams(next, { replace: true });
      })
      .catch((error: unknown) => {
        if (!cancelled) setLoadError(error instanceof Error ? error.message : String(error));
      });
    return () => {
      cancelled = true;
    };
  }, [sessionId, setParams, params]);

  useEffect(() => {
    if (!sessionId || loadedRef.current === sessionId) return;
    loadedRef.current = sessionId;
    setLoadError(null);
    getSession(sessionId)
      .then((detail) => {
        const { turns: history, ...meta } = detail;
        setSession(meta);
        loadHistory(detail); // 内部整体替换，历史轮次不会与上一会话混在一起
      })
      .catch((error: unknown) => {
        setLoadError(error instanceof Error ? error.message : String(error));
      });
  }, [sessionId, loadHistory]);

  const handleSend = useCallback(
    (question: string) => {
      if (!sessionId) return;
      // 提问后回取一次会话元数据：轮次只在加载时取过一次，不回取的话
      // 「已进行 N 轮」会一直停在进入页面时的数字
      void send(sessionId, question).then(() => {
        getSession(sessionId)
          .then((detail) =>
            setSession((prev) => (prev ? { ...prev, turn_count: detail.turn_count } : prev)),
          )
          .catch(() => undefined);
      });
    },
    [sessionId, send],
  );

  /**
   * 下钻（FR-L-2）：先把槽位换成问句，再走正常的提问流程。
   *
   * 不直接把问句拼在前端——下一层维度取自语义层配置，前端拼出来的问句可能与
   * 链路能查到的维度对不上。接口不可用时给出原因而不是静默失败：点了一个按钮
   * 却什么都没发生，比弹一句「无法下钻」更难排查。
   */
  const handleDrilldown = useCallback(
    async (slots: DrilldownRequest) => {
      try {
        const plan = await requestDrilldown(slots);
        if (!plan.available) {
          void message.info(plan.reason || '这一层已无法继续下钻');
          return;
        }
        handleSend(plan.question);
      } catch (error: unknown) {
        void message.error(error instanceof Error ? error.message : String(error));
      }
    },
    [handleSend],
  );

  // FR-O-4：告警中心「去对话里下钻」带 ?q= 跳转。会话就绪后自动发一次，
  // 然后把 q 从地址栏清掉，避免刷新 / 回退时重复发问。
  const pendingQuestion = params.get('q');
  useEffect(() => {
    if (!sessionId || !pendingQuestion) return;
    const token = `${sessionId}|${pendingQuestion}`;
    if (consumedQuestionRef.current === token) return;
    consumedQuestionRef.current = token;
    handleSend(pendingQuestion);
    const next = new URLSearchParams(params);
    next.delete('q');
    setParams(next, { replace: true });
    // handleSend 已覆盖依赖语义，params 只用于消费后清参
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId, pendingQuestion]);

  if (loadError) {
    return (
      <Alert
        type="error"
        showIcon
        message="会话加载失败"
        description={
          <Space direction="vertical">
            <Text>{loadError}</Text>
            <Button size="small" onClick={() => navigate('/', { replace: true })}>
              回到新会话
            </Button>
          </Space>
        }
      />
    );
  }

  return (
    <div style={{ display: 'flex', justifyContent: 'center', height: '100%', minHeight: 0 }}>
      <div
        style={{
          display: 'flex',
          flexDirection: 'column',
          height: '100%',
          minHeight: 0,
          width: '100%',
          maxWidth: 860,
        }}
      >
        {session && (
          <Text type="secondary" style={{ fontSize: 12, marginBottom: 8 }}>
            {session.title} · 已进行 {session.turn_count} 轮
          </Text>
        )}
        <div style={{ flex: 1, minHeight: 0 }}>
          <ChatPanel
            turns={turns}
            streaming={streaming}
            onSend={handleSend}
            onAbort={abort}
            onDrilldown={handleDrilldown}
            sessionId={sessionId ?? undefined}
          />
        </div>
      </div>
    </div>
  );
}
