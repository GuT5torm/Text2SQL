import { Button, Space, Tag, Tooltip, Typography } from 'antd';
import { AimOutlined } from '@ant-design/icons';
import type { Finding, FindingRule, FindingSeverity } from '@/api/types';
import { formatDeviation, formatNumber } from '@/lib/format';

const { Text } = Typography;

interface Props {
  findings: Finding[];
  /**
   * 点击「下钻」的回调。不传则不渲染下钻入口——历史回看没有可提交的会话，
   * 显示一个点不动的按钮不如不显示。
   */
  onDrilldown?: (finding: Finding) => void;
  /** 有请求在途时禁用入口，避免连点排出两条一样的问句 */
  disabled?: boolean;
}

const SEVERITY_COLOR: Record<FindingSeverity, string> = { 关注: 'orange', 提示: 'blue' };

/** 规则名按「判据」而不是「算法」命名：使用者关心的是凭什么标出来的。 */
const RULE_LABEL: Record<FindingRule, string> = {
  deviation: '偏离对比基准',
  rank: '组内排名异常',
  absolute: '越过业务阈值',
};

/**
 * 异常标记列表（FR-L-1）。
 *
 * 标记全部由服务端按 finding_rules.yaml 判定，前端不做任何阈值判断，也不
 * 重新排序——界面顺序与判定顺序不一致时，使用者无法判断哪一条更值得先看。
 */
export function FindingList({ findings, onDrilldown, disabled = false }: Props) {
  if (!findings.length) return null;

  return (
    <div style={{ marginTop: 8 }}>
      <Text type="secondary" style={{ fontSize: 12 }}>
        异常标记 {findings.length} 条
      </Text>
      <div style={{ marginTop: 4 }}>
        {findings.map((f, i) => {
          const drillable = Boolean(onDrilldown && f.dimension && f.slice !== null && f.slice !== undefined);
          return (
            <div
              key={`${f.rule}-${f.slice ?? i}-${i}`}
              style={{
                display: 'flex',
                alignItems: 'flex-start',
                gap: 8,
                padding: '6px 8px',
                marginBottom: 4,
                background: '#fafafa',
                border: '1px solid #f0f0f0',
                borderRadius: 6,
              }}
            >
              <Space size={4} style={{ flexShrink: 0 }}>
                <Tag
                  color={SEVERITY_COLOR[f.severity] ?? 'default'}
                  style={{ borderRadius: 4, marginInlineEnd: 0 }}
                >
                  {f.severity}
                </Tag>
                <Tooltip title={RULE_LABEL[f.rule] ?? f.rule}>
                  <Tag style={{ borderRadius: 4, marginInlineEnd: 0 }}>{RULE_LABEL[f.rule] ?? f.rule}</Tag>
                </Tooltip>
              </Space>

              <div style={{ flex: 1, minWidth: 0, fontSize: 13, lineHeight: 1.6 }}>
                {f.reason}
                <div>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    值 {formatNumber(f.value)}
                    {f.baseline !== null && f.baseline !== undefined && (
                      <> · 基准 {formatNumber(f.baseline)}</>
                    )}
                    {f.deviation !== null && f.deviation !== undefined && (
                      <> · 偏离 {formatDeviation(f.deviation)}</>
                    )}
                  </Text>
                </div>
              </div>

              {drillable && (
                <Button
                  size="small"
                  type="link"
                  icon={<AimOutlined />}
                  disabled={disabled}
                  style={{ flexShrink: 0, paddingInline: 4 }}
                  onClick={(e) => {
                    e.stopPropagation();
                    onDrilldown?.(f);
                  }}
                >
                  下钻
                </Button>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
