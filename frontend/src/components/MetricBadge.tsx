import { Space, Tag, Tooltip, Typography } from 'antd';
import type { BaselineWindow, Baselines, DataFreshness, TimeRange } from '@/api/types';
import { formatDeviation, formatNumber } from '@/lib/format';

const { Text } = Typography;

interface Props {
  metricUsed: string[];
  caveats: string[];
  timeRange?: TimeRange | null;
  /** 对比基准（FR-M-1）。为 null 时本组件只呈现口径，不出现基准相关的标签 */
  baselines?: Baselines | null;
  /** 本期是否不完整（FR-M-2）。与 baselines.partial_period 同源，取其一即可 */
  partialPeriod?: boolean;
  /**
   * SQL 时间谓词与召回区间是否一致（FR-D-5）。
   * 仅 false 时显式橙色告警；null（未核对）不渲染，避免把「没核对」说成「不一致」。
   */
  timeRangeConsistent?: boolean | null;
  /**
   * 数据时效（FR-D-2）。此前该字段只在后端与类型声明里存在，界面没有消费点，
   * 使用者看不到「数据到哪一天」。为 null 时（历史回看、老库记录）整项不渲染。
   */
  dataFreshness?: DataFreshness | null;
}

/** 距今天数。按本地日期零点计算，避免出现「0 天前」其实是 23 小时前的歧义。 */
function daysSince(day: string): number | null {
  const d = new Date(`${day.slice(0, 10)}T00:00:00`);
  if (Number.isNaN(d.getTime())) return null;
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  return Math.max(0, Math.round((today.getTime() - d.getTime()) / 86_400_000));
}

/**
 * 数据截至标签。两张表不同步时取较早者作为统一截止点——较晚的那张不能代表
 * 整库可用范围，取它会让「数据到 9-14」这句在订单表上不成立。
 */
function FreshnessTag({ freshness }: { freshness: DataFreshness }) {
  const candidates = [freshness.orders_cutoff, freshness.refunds_cutoff].filter(
    (v): v is string => Boolean(v),
  );
  if (!candidates.length) return null;

  const cutoff = [...candidates].sort()[0];
  const day = cutoff.slice(0, 10);
  const age =
    daysSince(day) ?? Math.max(freshness.orders_age_days ?? 0, freshness.refunds_age_days ?? 0);
  const stale = Boolean(freshness.is_stale);

  const lines = [`业务库数据截至 ${day}（${age} 天前）`];
  if (
    freshness.orders_cutoff &&
    freshness.refunds_cutoff &&
    freshness.orders_cutoff.slice(0, 10) !== freshness.refunds_cutoff.slice(0, 10)
  ) {
    lines.push(
      `订单表 ${freshness.orders_cutoff.slice(0, 10)}，退款表 ${freshness.refunds_cutoff.slice(0, 10)}，取较早者`,
    );
  }
  if (stale) {
    lines.push(`已超过配置的时效阈值 ${freshness.threshold_days ?? '—'} 天`);
    if (freshness.note) lines.push(freshness.note);
  }

  return (
    <Tooltip title={<span style={{ whiteSpace: 'pre-line' }}>{lines.join('\n')}</span>}>
      <Tag color={stale ? 'red' : 'default'} style={{ borderRadius: 4, marginInlineEnd: 0 }}>
        数据截至 {day}
        {age > 0 ? `（${age} 天前）` : ''}
      </Tag>
    </Tooltip>
  );
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
 * 单个基准窗口的提示文案。把本期值、对比期值与窗口一起给出，
 * 否则「环比 +5.6%」这句话缺少被比较的那一半，无法核对。
 */
function windowTooltip(win: BaselineWindow, current: number | null | undefined) {
  const lines = [`${win.label}对比期：${win.start} ~ ${win.end}`];
  if (win.value !== null && win.value !== undefined) lines.push(`对比期值：${formatNumber(win.value)}`);
  if (current !== null && current !== undefined) lines.push(`本期值：${formatNumber(current)}`);
  if (win.deviation === null || win.deviation === undefined) {
    lines.push('偏离度不可比，不参与结论生成');
  }
  return lines.join('\n');
}

/** 基准标签。不可比时只呈现「哪些窗口算出来了」，不给出偏离度。 */
function BaselineTag({ win, current }: { win: BaselineWindow; current: number | null | undefined }) {
  const comparable = win.comparable !== false && win.deviation !== null && win.deviation !== undefined;
  const up = (win.deviation ?? 0) >= 0;
  // 涨红跌绿。这里的红绿只表示方向，不表示好坏——退款率上升同样是红色，
  // 判断由使用者做，界面不代替使用者下结论。
  const color = !comparable ? 'default' : up ? 'red' : 'green';
  const text = comparable ? `${win.label} ${formatDeviation(win.deviation)}` : `${win.label} 不可比`;

  return (
    <Tooltip title={<span style={{ whiteSpace: 'pre-line' }}>{windowTooltip(win, current)}</span>}>
      <Tag color={color} style={{ borderRadius: 4, marginInlineEnd: 0 }}>
        {text}
      </Tag>
    </Tooltip>
  );
}

/**
 * 本轮口径标签。内容取自后端返回的 metric_used、caveats 与 baselines，
 * 前端不做任何口径推断，避免出现「界面上写着某口径但 SQL 并非如此」的情况。
 */
export function MetricBadge({
  metricUsed,
  caveats,
  timeRange,
  baselines,
  partialPeriod,
  timeRangeConsistent,
  dataFreshness,
}: Props) {
  const hasBaseline = Boolean(baselines);
  if (!metricUsed.length && !caveats.length && !timeRange && !hasBaseline && !dataFreshness) {
    return null;
  }

  const partial = baselines?.partial_period ?? partialPeriod ?? false;
  const note = baselines?.note ?? '';
  const reasons = baselines?.missing_reasons ?? [];

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

      {/* FR-D-2：数据截至常驻。与指标标签同处一行，刷新或回看时都能看到 */}
      {dataFreshness && <FreshnessTag freshness={dataFreshness} />}

      {timeRangeConsistent === false && (
        <Tooltip title="实际执行 SQL 的时间谓词与语义层召回的时间区间不一致，请展开「技术细节」核对 SQL 时间条件。">
          <Tag color="orange" style={{ borderRadius: 4, marginInlineEnd: 0 }}>
            时间区间不一致
          </Tag>
        </Tooltip>
      )}

      {baselines?.mom && (
        <BaselineTag win={baselines.mom} current={baselines.current_value} />
      )}
      {baselines?.yoy && (
        <BaselineTag win={baselines.yoy} current={baselines.current_value} />
      )}

      {partial && (
        <Tooltip title={note || '本期尚未走完，对比期数值仅供标注，不参与结论生成'}>
          <Tag color="gold" style={{ borderRadius: 4, marginInlineEnd: 0 }}>
            本期未完整
          </Tag>
        </Tooltip>
      )}

      {/* 无基准时必须说明为什么没有。留白会被读成「忘了算」而不是「算不出来」 */}
      {!baselines?.mom && !baselines?.yoy && note && (
        <Tooltip
          title={
            reasons.length ? (
              <span style={{ whiteSpace: 'pre-line' }}>{reasons.join('\n')}</span>
            ) : (
              note
            )
          }
        >
          <Tag style={{ borderRadius: 4, marginInlineEnd: 0 }}>
            <Text type="secondary">{note}</Text>
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
