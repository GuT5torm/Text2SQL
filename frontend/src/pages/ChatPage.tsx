import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { Alert, Button, Card, Input, Space, Tooltip, Typography, message } from 'antd';
import { PlusOutlined } from '@ant-design/icons';
import { ChatPanel } from '@/components/ChatPanel';
import { ResultPane } from '@/components/ResultPane';
import { SplitPane } from '@/components/SplitPane';
import { useChatStream } from '@/hooks/useChatStream';
import { requestDrilldown } from '@/api/drilldown';
import { createSession, getSession, renameSession } from '@/api/session';
import type { DrilldownRequest, SessionItem } from '@/api/types';

const { Text } = Typography;

/**
 * 会话是否被换掉由 session_id 决定：换 id 就 reload 历史，
 * 同一个会话内的多轮由 useChatStream 自己累积，不重复拉取。
 */
export function ChatPage() {
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const sessionId = params.get('session');

  const [session, setSession] = useState<SessionItem | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [titleDraft, setTitleDraft] = useState('');
  const [focusKey, setFocusKey] = useState<string | null>(null);

  const { turns, streaming, send, abort, reset, loadHistory } = useChatStream();
  const lastKey = turns.length ? turns[turns.length - 1].key : null;
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
        setFocusKey(history.length ? `turn-${history[history.length - 1].id}` : null);
      })
      .catch((error: unknown) => {
        setLoadError(error instanceof Error ? error.message : String(error));
      });
  }, [sessionId, loadHistory]);

  // 新一轮开始或历史加载完成后，右侧始终跟随最新一轮
  useEffect(() => {
    if (lastKey) setFocusKey(lastKey);
  }, [lastKey]);

  const focused = useMemo(
    () => turns.find((t) => t.key === focusKey) ?? (turns.length ? turns[turns.length - 1] : null),
    [turns, focusKey],
  );

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

  const handleNew = useCallback(() => {
    createSession()
      .then((rec) => {
        reset();
        setFocusKey(null);
        setParams({ session: rec.id });
      })
      .catch((error: unknown) => {
        void message.error(error instanceof Error ? error.message : String(error));
      });
  }, [reset, setParams]);

  const commitTitle = useCallback(() => {
    if (!sessionId) return;
    const next = titleDraft.trim();
    setEditing(false);
    if (!next || next === session?.title) return;
    renameSession(sessionId, next)
      .then(setSession)
      .catch((error: unknown) => {
        void message.error(error instanceof Error ? error.message : String(error));
      });
  }, [sessionId, titleDraft, session?.title]);

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
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0 }}>
      <Card size="small" style={{ borderRadius: 8, marginBottom: 12 }} styles={{ body: { padding: '8px 12px' } }}>
        <Space style={{ width: '100%', justifyContent: 'space-between' }}>
          {editing ? (
            <Input
              size="small"
              autoFocus
              value={titleDraft}
              onChange={(e) => setTitleDraft(e.target.value)}
              onBlur={commitTitle}
              onPressEnter={commitTitle}
              style={{ width: 260 }}
              maxLength={120}
            />
          ) : (
            <Tooltip title="点击重命名">
              <Text
                strong
                style={{ cursor: 'pointer' }}
                onClick={() => {
                  setTitleDraft(session?.title ?? '新会话');
                  setEditing(true);
                }}
              >
                {session?.title ?? '新建会话'}
              </Text>
            </Tooltip>
          )}
          <Space size={8}>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {session ? `已进行 ${session.turn_count} 轮` : '尚未开始'}
            </Text>
            <Button size="small" icon={<PlusOutlined />} onClick={handleNew}>
              新建会话
            </Button>
          </Space>
        </Space>
      </Card>

      <div style={{ flex: 1, minHeight: 0 }}>
        <SplitPane
          left={
            <ChatPanel
              turns={turns}
              streaming={streaming}
              onSend={handleSend}
              onAbort={abort}
              onFocus={setFocusKey}
              focusedKey={focusKey}
              onDrilldown={handleDrilldown}
              sessionId={sessionId ?? undefined}
            />
          }
          right={<ResultPane turn={focused} onDrilldown={handleDrilldown} />}
        />
      </div>
    </div>
  );
}
