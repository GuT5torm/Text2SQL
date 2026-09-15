import { Space, Tag, Tooltip } from 'antd';
import type { TimeRange } from '@/api/types';

interface Props {
  metricUsed: string[];
  caveats: string[];
  timeRange?: TimeRange | null;
}

/**
 * 口径标签的文本形式。后端语义层的 caveats 一律写成「指标名：口径描述」，
 * 全串放进 Tag 会把卡片撑满，这里按冒号拆开：标签留指标名，完整口径进提示。
 */
function splitCaveat(text: string): { label: string; full: string } {
  const idx = text.indexOf('：');
  if (idx === -1) return { label: text, full: text };
  const label = text.slice(0, idx);
  return { label: label.length > 10 ? `${label.slice(0, 10)}…` : label, full: text };
}

/**
 * 本轮口径标签。内容取自后端返回的 metric_used 与 caveats，
 * 前端不做任何口径推断，避免出现「界面上写着某口径但 SQL 并非如此」的情况。
 */
export function MetricBadge({ metricUsed, caveats, timeRange }: Props) {
  if (!metricUsed.length && !caveats.length && !timeRange) return null;

  return (
    <Space size={[6, 6]} wrap>
      {metricUsed.map((name) => (
        <Tag key={`m-${name}`} color="blue" style={{ borderRadius: 4, marginInlineEnd: 0 }}>
          {name}
        </Tag>
      ))}
      {timeRange?.label && (
        <Tooltip title={timeRange.start && timeRange.end ? `${timeRange.start} ~ ${timeRange.end}` : ''}>
          <Tag color="geekblue" style={{ borderRadius: 4, marginInlineEnd: 0 }}>
            {timeRange.label}
          </Tag>
        </Tooltip>
      )}
      {caveats.map((text) => {
        const { label, full } = splitCaveat(text);
        return (
          <Tooltip key={`c-${text}`} title={full}>
            <Tag color="orange" style={{ borderRadius: 4, marginInlineEnd: 0 }}>
              {label}
            </Tag>
          </Tooltip>
        );
      })}
    </Space>
  );
}
