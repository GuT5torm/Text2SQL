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
 *
 * 会话采用惰性创建：进入页面、点「新建对话」都不落库，只有发出第一句提问时
 * 才建会话。此前是「无 session 参数即建会话」，导致每次打开页面都留下一条空
 * 「新会话」，侧边栏越积越多。
 */
interface Props {
  /** 一轮问答落库后回调，用于让侧边栏刷新列表（标题 / 轮次此时才定下来） */
  onTurnComplete?: () => void;
}

export function ChatPage({ onTurnComplete }: Props) {
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const sessionId = params.get('session');

  const [session, setSession] = useState<SessionItem | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const { turns, streaming, send, abort, reset, loadHistory } = useChatStream();
  const loadedRef = useRef<string | null>(null);
  // FR-O-4：告警中心一键下钻带 ?q= 跳来，同一问句只自动发送一次
  const consumedQuestionRef = useRef<string | null>(null);

  // 无会话时清空对话区：点「新建对话」只是导航到不带参数的路由，不建会话，
  // 这里把上一个会话的轮次清掉，避免残留。
  useEffect(() => {
    if (sessionId) return;
    loadedRef.current = null;
    setSession(null);
    setLoadError(null);
    reset();
  }, [sessionId, reset]);

  // 载入已有会话
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

  /**
   * 统一的提问入口：没有会话时先建一个再发，并把地址栏的 ?q= 一并清掉，
   * 发完回取一次会话元数据刷新「已进行 N 轮」。
   *
   * 建完会话要把 loadedRef 指向新 id，让上面的载入 effect 跳过——新会话本
   * 就没有历史，而载入动作里的 loadHistory 会 abortAll 并把 turns 整体替换，
   * 恰好会把刚发出、还在流式中的这一轮冲掉。
   */
  const startQuestion = useCallback(
    async (question: string) => {
      let sid = sessionId;
      if (!sid) {
        try {
          const rec = await createSession();
          sid = rec.id;
          loadedRef.current = rec.id;
          setSession(rec);
          // 一次更新里写入新会话并清掉 ?q=，避免两次 setParams 互相覆盖
          const next = new URLSearchParams(params);
          next.delete('q');
          next.set('session', rec.id);
          setParams(next, { replace: true });
        } catch (error: unknown) {
          setLoadError(error instanceof Error ? error.message : String(error));
          return;
        }
      } else if (params.get('q')) {
        const next = new URLSearchParams(params);
        next.delete('q');
        setParams(next, { replace: true });
      }

      await send(sid, question);
      // 落库后通知侧边栏刷新：标题（后端在首轮落库时才把默认「新会话」改成问句）
      // 与轮次都在这一刻才定下来。
      onTurnComplete?.();
      // 同时回取一次会话元数据，把页面上方的「标题 · 已进行 N 轮」同步过来
      getSession(sid)
        .then((detail) => {
          setSession((prev) =>
            prev && prev.id === sid
              ? { ...prev, title: detail.title, turn_count: detail.turn_count }
              : prev,
          );
        })
        .catch(() => undefined);
    },
    [sessionId, params, setParams, send, onTurnComplete],
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
        void startQuestion(plan.question);
      } catch (error: unknown) {
        void message.error(error instanceof Error ? error.message : String(error));
      }
    },
    [startQuestion],
  );

  // FR-O-4：告警中心「去对话里下钻」带 ?q= 跳转。自动发一次——会话缺失时由
  // startQuestion 惰性创建；?q= 的清理也一并交给它，避免两处 setParams 互相覆盖。
  const pendingQuestion = params.get('q');
  useEffect(() => {
    if (!pendingQuestion) return;
    if (consumedQuestionRef.current === pendingQuestion) return;
    consumedQuestionRef.current = pendingQuestion;
    void startQuestion(pendingQuestion);
  }, [pendingQuestion, startQuestion]);

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
            onSend={startQuestion}
            onAbort={abort}
            onDrilldown={handleDrilldown}
            sessionId={sessionId ?? undefined}
          />
        </div>
      </div>
    </div>
  );
}
