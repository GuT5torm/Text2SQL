import { useEffect, useRef, useState } from 'react';
import { Button, Empty, Input, Space, Tag, Typography } from 'antd';
import { SendOutlined } from '@ant-design/icons';
import type { ChatTurn } from '@/hooks/useChatStream';
import { AnswerCard } from './AnswerCard';

const { Text } = Typography;

interface Props {
  turns: ChatTurn[];
  streaming: boolean;
  onSend: (question: string) => void;
  onAbort: () => void;
  /** 点击历史卡片切换右侧结果区展示的轮次 */
  onFocus?: (key: string) => void;
  focusedKey?: string | null;
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
export function ChatPanel({ turns, streaming, onSend, onAbort, onFocus, focusedKey }: Props) {
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
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={
              <Space direction="vertical" size={6}>
                <Text type="secondary">还没有提问，可以从下面选一个</Text>
                {EXAMPLES.map((q) => (
                  <Tag
                    key={q}
                    style={{ cursor: 'pointer', borderRadius: 4 }}
                    onClick={() => !streaming && onSend(q)}
                  >
                    {q}
                  </Tag>
                ))}
              </Space>
            }
            style={{ marginTop: 48 }}
          />
        )}

        {turns.map((turn) => (
          <div key={turn.key} style={{ marginBottom: 16 }}>
            <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 8 }}>
              <div
                style={{
                  maxWidth: '86%',
                  padding: '8px 12px',
                  background: '#1677ff',
                  color: '#fff',
                  borderRadius: 8,
                  fontSize: 14,
                  lineHeight: 1.6,
                  whiteSpace: 'pre-wrap',
                  wordBreak: 'break-word',
                }}
              >
                {turn.question}
              </div>
            </div>
            <AnswerCard
              turn={turn}
              compact
              focused={turn.key === focusedKey}
              onFocus={onFocus ? () => onFocus(turn.key) : undefined}
              onRetry={streaming ? undefined : () => onSend(turn.question)}
            />
          </div>
        ))}
        <div ref={bottomRef} />
      </div>

      <div style={{ borderTop: '1px solid #f0f0f0', paddingTop: 8 }}>
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
