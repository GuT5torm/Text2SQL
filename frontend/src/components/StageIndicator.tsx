import { useEffect, useMemo, useRef, useState } from 'react';
import { Space, Tag, Typography } from 'antd';
import type { StageEvent } from '@/api/types';

const { Text } = Typography;

/** 终态节点名 → 展示色。普通节点统一用主色，终态区分成功 / 反问 / 失败。 */
const TERMINAL_COLOR: Record<string, string> = {
  respond: 'success',
  clarify: 'default',
  give_up: 'error',
};

/** 同一节点在 fix 回环中可能出现多次，用出现次数做后缀区分。 */
function withOccurrences(stages: StageEvent[]): { stage: StageEvent; seq: number; total: number }[] {
  const counter = new Map<string, number>();
  const total = new Map<string, number>();
  stages.forEach((s) => total.set(s.node, (total.get(s.node) ?? 0) + 1));
  return stages.map((stage) => {
    const n = (counter.get(stage.node) ?? 0) + 1;
    counter.set(stage.node, n);
    return { stage, seq: n, total: total.get(stage.node) ?? n };
  });
}

interface Props {
  stages: StageEvent[];
  running: boolean;
}

/**
 * 阶段指示。按 SSE 到达顺序追加，不重排 —— 顺序本身携带了执行路径信息，
 * 出现 fix 就说明中间修正过。完成后默认收起为一行。
 */
export function StageIndicator({ stages, running }: Props) {
  const [expanded, setExpanded] = useState(false);
  const wasRunning = useRef(running);

  // 新一轮开始（从非运行切到运行）时重新展开，让进度可见
  useEffect(() => {
    if (running && !wasRunning.current) setExpanded(true);
    wasRunning.current = running;
  }, [running]);

  const items = useMemo(() => withOccurrences(stages), [stages]);
  if (!items.length) return null;

  const last = items[items.length - 1].stage;
  const totalMs = stages.reduce((sum, s) => sum + (s.elapsed_ms ?? 0), 0);

  if (!expanded && !running) {
    return (
      <div
        onClick={() => setExpanded(true)}
        style={{ cursor: 'pointer', padding: '2px 0', lineHeight: 1.6 }}
      >
        <Space size={8} wrap>
          <Tag color={TERMINAL_COLOR[last.node] ?? 'success'} style={{ borderRadius: 4, marginInlineEnd: 0 }}>
            {last.label}
          </Tag>
          <Text type="secondary" style={{ fontSize: 12 }}>
            共 {stages.length} 个阶段 · {totalMs.toFixed(0)} ms · 点击展开
          </Text>
        </Space>
      </div>
    );
  }

  return (
    <div style={{ padding: '2px 0' }}>
      <Space direction="vertical" size={4} style={{ width: '100%' }}>
        {items.map(({ stage, seq, total }, index) => (
          <div key={`${stage.node}-${index}`} style={{ display: 'flex', gap: 8, alignItems: 'baseline' }}>
            <Tag
              color={TERMINAL_COLOR[stage.node] ?? 'processing'}
              style={{ borderRadius: 4, marginInlineEnd: 0, minWidth: 68, textAlign: 'center' }}
            >
              {total > 1 ? `${stage.label} ×${seq}` : stage.label}
            </Tag>
            <Text style={{ fontSize: 13 }}>{stage.detail}</Text>
            <Text type="secondary" style={{ fontSize: 12, marginLeft: 'auto', whiteSpace: 'nowrap' }}>
              {(stage.elapsed_ms ?? 0).toFixed(0)} ms
            </Text>
          </div>
        ))}
        {running && (
          <Text type="secondary" style={{ fontSize: 12 }}>
            处理中…
          </Text>
        )}
      </Space>
      {!running && (
        <Text
          type="secondary"
          style={{ fontSize: 12, cursor: 'pointer', display: 'inline-block', marginTop: 4 }}
          onClick={() => setExpanded(false)}
        >
          收起
        </Text>
      )}
    </div>
  );
}
