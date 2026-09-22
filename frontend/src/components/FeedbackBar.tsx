import { useEffect, useState } from 'react';
import { Button, Input, Modal, Radio, Space, Typography, message } from 'antd';
import { DislikeOutlined, LikeOutlined } from '@ant-design/icons';
import { submitFeedback } from '@/api/feedback';
import type { FeedbackItem, FeedbackRating, FeedbackReasonCode } from '@/api/types';

const { Text } = Typography;

const REASON_OPTIONS: { label: string; value: FeedbackReasonCode }[] = [
  { label: '口径错', value: 'wrong_metric' },
  { label: '字段错', value: 'wrong_field' },
  { label: '数字错', value: 'wrong_number' },
  { label: '答非所问', value: 'off_topic' },
  { label: '其他', value: 'other' },
];

interface Props {
  turnId: number;
  sessionId: string;
  /** 历史回看时回填已有的反馈，避免用户重复提交时看不到当前状态 */
  existing?: FeedbackItem | null;
  /** 提交成功后的回调，让父组件刷新回看态（用于历史页落地反馈后回拉） */
  onChanged?: (item: FeedbackItem) => void;
}

/**
 * 一轮回答底部的点赞点踩（FR-G-1）。
 *
 * 同一 turn_id 重复提交走更新：先点踩、后改成点赞，后端只保留最新一条。
 * 提交点踩时弹出原因选择与「正确问法」输入框（FR-G-2）。
 */
export function FeedbackBar({ turnId, sessionId, existing, onChanged }: Props) {
  const [rating, setRating] = useState<FeedbackRating | null>(existing?.rating ?? null);
  const [downOpen, setDownOpen] = useState(false);
  const [reason, setReason] = useState<FeedbackReasonCode>(existing?.reason_code ?? '');
  const [note, setNote] = useState(existing?.note ?? '');
  const [submitting, setSubmitting] = useState(false);

  // 父组件回填变化（切到另一轮回看时）时同步本地状态
  useEffect(() => {
    setRating(existing?.rating ?? null);
    setReason(existing?.reason_code ?? '');
    setNote(existing?.note ?? '');
  }, [existing?.id, turnId]);

  const submit = async (next: FeedbackRating, reasonCode?: FeedbackReasonCode, noteText?: string) => {
    setSubmitting(true);
    try {
      const res = await submitFeedback({
        turn_id: turnId,
        session_id: sessionId,
        rating: next,
        reason_code: reasonCode,
        note: noteText,
      });
      setRating(next);
      onChanged?.(res.feedback);
      void message.success(next === 'up' ? '已点赞' : '已记录你的反馈');
    } catch (err) {
      void message.error(err instanceof Error ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  };

  const onPick = (next: FeedbackRating) => {
    if (next === 'up') {
      void submit('up', '', '');
      return;
    }
    // 点踩要先选原因：直接提交会让原因缺失
    setDownOpen(true);
  };

  const confirmDown = () => {
    const finalReason: FeedbackReasonCode = reason || 'other';
    setDownOpen(false);
    void submit('down', finalReason, note.trim());
  };

  return (
    <div style={{ marginTop: 8, borderTop: '1px dashed #f0f0f0', paddingTop: 6 }}>
      <Space size={8}>
        <Text type="secondary" style={{ fontSize: 12 }}>
          这条回答对你有帮助吗？
        </Text>
        <Button
          size="small"
          type={rating === 'up' ? 'primary' : 'default'}
          icon={<LikeOutlined />}
          onClick={() => onPick('up')}
          loading={submitting}
          disabled={submitting}
        >
          点赞
        </Button>
        <Button
          size="small"
          danger={rating === 'down'}
          type={rating === 'down' ? 'primary' : 'default'}
          icon={<DislikeOutlined />}
          onClick={() => onPick('down')}
          loading={submitting}
          disabled={submitting}
        >
          点踩
        </Button>
        {rating && (
          <Text type="secondary" style={{ fontSize: 12 }}>
            {rating === 'up' ? '已标记为有帮助' : '已标记为有问题'}
          </Text>
        )}
      </Space>

      <Modal
        title="这条回答哪里有问题？"
        open={downOpen}
        onOk={confirmDown}
        onCancel={() => setDownOpen(false)}
        okText="提交反馈"
        cancelText="取消"
        destroyOnClose
      >
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Text type="secondary" style={{ fontSize: 12 }}>
            选择最贴近的原因，便于后续校准口径。原因与你的备注只用于评测改进，不会影响本次回答。
          </Text>
          <Radio.Group
            value={reason || 'other'}
            onChange={(e) => setReason(e.target.value as FeedbackReasonCode)}
          >
            <Space direction="vertical">
              {REASON_OPTIONS.map((opt) => (
                <Radio key={opt.value} value={opt.value}>
                  {opt.label}
                </Radio>
              ))}
            </Space>
          </Radio.Group>
          <Input.TextArea
            value={note}
            onChange={(e) => setNote(e.target.value)}
            autoSize={{ minRows: 2, maxRows: 4 }}
            maxLength={500}
            placeholder="如果知道正确的问法或口径，可以在这里填写（选填，最多 500 字）"
          />
        </Space>
      </Modal>
    </div>
  );
}
