import { useEffect, useRef, useState } from 'react';
import { Button, Input, Space, Typography } from 'antd';
import { SendOutlined } from '@ant-design/icons';
import type { ChatTurn } from '@/hooks/useChatStream';
import type { DrilldownRequest } from '@/api/types';
import { AnswerCard } from './AnswerCard';

const { Text } = Typography;

interface Props {
  turns: ChatTurn[];
  streaming: boolean;
  onSend: (question: string) => void;
  onAbort: () => void;
  /** 下钻入口（FR-L-2）。由页面把槽位换成问句后重新提交 */
  onDrilldown?: (slots: DrilldownRequest) => void;
  /** 当前会话 ID，透传给 AnswerCard 让其渲染 FeedbackBar / ExportMenu */
  sessionId?: string;
}

const EXAMPLES = [
  '上个季度华东区各品类的成交总额',
  '各渠道近三个月的订单量',
  '今年以来各区域的退款率',
];

/**
 * 对话区。发送后禁用输入直到收到 result 或 error ——
 * 后端是单轮串行的，允许连续提交只会让两轮的进度事件混在一起。
 */
export function ChatPanel({
  turns,
  streaming,
  onSend,
  onAbort,
  onDrilldown,
  sessionId,
}: Props) {
  const [draft, setDraft] = useState('');
  const bottomRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' });
  }, [turns]);

  const submit = () => {
    const text = draft.trim();
    if (!text || streaming) return;
    setDraft('');
    onSend(text);
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0 }}>
      <div style={{ flex: 1, overflowY: 'auto', padding: '4px 4px 12px', minHeight: 0 }}>
        {turns.length === 0 && (
          <div style={{ maxWidth: 620, margin: '9vh auto 0', textAlign: 'center', padding: '0 8px' }}>
            <div
              style={{
                width: 52,
                height: 52,
                borderRadius: 14,
                background: 'linear-gradient(135deg, #2f6bff, #6f9bff)',
                display: 'inline-flex',
                alignItems: 'center',
                justifyContent: 'center',
                color: '#fff',
                fontWeight: 700,
                fontSize: 20,
                marginBottom: 18,
              }}
            >
              T2
            </div>
            <div style={{ fontSize: 22, fontWeight: 600, color: 'var(--wb-text)', marginBottom: 8 }}>
              用自然语言分析你的业务数据
            </div>
            <Text type="secondary" style={{ fontSize: 14 }}>
              描述一个经营问题，自动生成 SQL、图表与口径说明。可以从下面的示例开始。
            </Text>
            <div
              style={{
                display: 'grid',
                gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))',
                gap: 10,
                marginTop: 26,
              }}
            >
              {EXAMPLES.map((q) => (
                <div key={q} className="wb-suggest" onClick={() => !streaming && onSend(q)}>
                  <Text style={{ fontSize: 13, color: 'var(--wb-text)' }}>{q}</Text>
                </div>
              ))}
            </div>
          </div>
        )}

        {turns.map((turn) => (
          <div key={turn.key} style={{ marginBottom: 16 }}>
            <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 8 }}>
              <div
                style={{
                  maxWidth: '82%',
                  padding: '9px 14px',
                  background: 'var(--wb-primary)',
                  color: '#fff',
                  borderRadius: 12,
                  fontSize: 14,
                  lineHeight: 1.6,
                  whiteSpace: 'pre-wrap',
                  wordBreak: 'break-word',
                  boxShadow: '0 1px 2px rgba(47, 107, 255, 0.25)',
                }}
              >
                {turn.question}
              </div>
            </div>
            <AnswerCard
              turn={turn}
              onRetry={streaming ? undefined : () => onSend(turn.question)}
              onDrilldown={onDrilldown}
              sessionId={sessionId}
            />
          </div>
        ))}
        <div ref={bottomRef} />
      </div>

      <div style={{ borderTop: '1px solid var(--wb-border)', paddingTop: 10, paddingBottom: 4 }}>
        <Space.Compact style={{ width: '100%' }}>
          <Input.TextArea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onPressEnter={(e) => {
              if (!e.shiftKey) {
                e.preventDefault();
                submit();
              }
            }}
            autoSize={{ minRows: 2, maxRows: 6 }}
            maxLength={500}
            placeholder="输入经营分析问题，Enter 发送，Shift+Enter 换行"
            disabled={streaming}
            style={{ borderRadius: 6 }}
          />
          <Button
            type="primary"
            icon={<SendOutlined />}
            onClick={submit}
            loading={streaming}
            disabled={streaming}
          >
            {streaming ? '' : '发送'}
          </Button>
        </Space.Compact>
        {streaming && (
          <div style={{ marginTop: 6, textAlign: 'right' }}>
            <Button size="small" type="link" onClick={onAbort}>
              中断本次请求
            </Button>
          </div>
        )}
      </div>
    </div>
  );
}
