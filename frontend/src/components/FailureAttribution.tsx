import { Space, Tag, Typography } from 'antd';
import type { FailureAttribution } from '@/api/types';

const { Text } = Typography;

/** 按归因编码配色。generate 是兜底那一类，用最中性的灰，不暗示具体故障位置 */
const COLORS: Record<FailureAttribution['kind'], string> = {
  guard: 'volcano',
  signature: 'gold',
  slot: 'orange',
  generate: 'default',
};

interface Props {
  /** 后端 failure_attribution。成功轮与老库记录为 null / undefined */
  attribution?: FailureAttribution | null;
  /** 没有归因时的兜底文案，由调用方按场景给 */
  fallback?: string;
}

/**
 * 失败归因说明。
 *
 * 归因编码与 eval 报表同一套（guard / signature / slot / generate），四类对应
 * 四种修法。界面上必须同时给标签与修法：只显示一句「请重试」等于让用户自己
 * 去猜该改什么，而报表里明明已经知道这一轮是口径问题还是条件没落进语句。
 */
export function FailureAttributionNotice({ attribution, fallback }: Props) {
  if (!attribution) {
    return fallback ? (
      <Text style={{ fontSize: 13 }}>{fallback}</Text>
    ) : null;
  }

  return (
    <Space direction="vertical" size={2} style={{ width: '100%' }}>
      <Space size={6} wrap>
        <Tag color={COLORS[attribution.kind] ?? 'default'} style={{ borderRadius: 4, marginInlineEnd: 0 }}>
          归因：{attribution.label}
        </Tag>
        <Text style={{ fontSize: 13 }}>修法：{attribution.suggestion}</Text>
      </Space>
      {attribution.evidence && (
        <Text type="secondary" style={{ fontSize: 12 }}>
          依据：{attribution.evidence}
        </Text>
      )}
    </Space>
  );
}
