import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Badge,
  Button,
  Card,
  Form,
  InputNumber,
  Modal,
  Popconfirm,
  Radio,
  Select,
  Space,
  Switch,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd';
import {
  DeleteOutlined,
  EditOutlined,
  ExperimentOutlined,
  PlusOutlined,
  ReloadOutlined,
} from '@ant-design/icons';
import {
  deleteAlertRule,
  fetchAlertEvents,
  fetchAlertRules,
  markAlertEventSeen,
  runAlertCheck,
  upsertAlertRule,
} from '@/api/alerts';
import { fetchMetrics, type MetricDoc } from '@/api/semantic';
import type {
  AlertComparison,
  AlertEvent,
  AlertRule,
  AlertRuleBody,
  AlertSeverity,
} from '@/api/types';

const { Text, Paragraph } = Typography;

const COMPARISON_OPTIONS: { value: AlertComparison; label: string; hint: string }[] = [
  { value: 'gt', label: '高于阈值', hint: '指标值 > 阈值时命中（如退款率高于 10%）' },
  { value: 'lt', label: '低于阈值', hint: '指标值 < 阈值时命中' },
  {
    value: 'deviation',
    label: '偏离度超过',
    hint: '与对比基准的相对偏离超过阈值百分比，仅支持整体口径，不能按维度切片',
  },
];

const SEVERITY_OPTIONS: { value: AlertSeverity; label: string }[] = [
  { value: 'notice', label: '提示' },
  { value: 'attention', label: '关注' },
];

function formatSlice(event: AlertEvent, hitIndex: number): string {
  const hit = event.hits[hitIndex];
  if (!hit) return '';
  if (!hit.slice) return '整体';
  return Object.entries(hit.slice)
    .map(([k, v]) => `${k}=${String(v)}`)
    .join('，');
}

/**
 * 异常订阅中心（FR-O-1 ~ FR-O-5）。
 * 上半屏管规则（CRUD + 立即求值），下半屏看事件（命中 / 失败、已读、一键下钻）。
 * 进程内没有调度器：周期求值依赖外部计划任务调 /api/alert/check，本页只做手动入口。
 */
export function AlertCenter() {
  const navigate = useNavigate();
  const [rules, setRules] = useState<AlertRule[]>([]);
  const [events, setEvents] = useState<AlertEvent[]>([]);
  const [unread, setUnread] = useState(0);
  const [metrics, setMetrics] = useState<MetricDoc[]>([]);
  const [seenFilter, setSeenFilter] = useState<boolean | undefined>(undefined);
  const [ruleFilter, setRuleFilter] = useState<number | undefined>(undefined);
  const [loading, setLoading] = useState(false);
  const [checking, setChecking] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<AlertRule | null>(null);
  const [form] = Form.useForm<AlertRuleBody>();

  const loadRules = useCallback(async () => {
    const list = await fetchAlertRules(false);
    setRules(list);
  }, []);

  const loadEvents = useCallback(async () => {
    const res = await fetchAlertEvents({ seen: seenFilter, ruleId: ruleFilter, limit: 200 });
    setEvents(res.events);
    setUnread(res.unread);
  }, [seenFilter, ruleFilter]);

  useEffect(() => {
    Promise.all([fetchMetrics().catch(() => [] as MetricDoc[]), loadRules()])
      .then(([metricList]) => setMetrics(metricList))
      .catch((error: unknown) => {
        void message.error(error instanceof Error ? error.message : String(error));
      });
  }, [loadRules]);

  useEffect(() => {
    loadEvents().catch((error: unknown) => {
      void message.error(error instanceof Error ? error.message : String(error));
    });
  }, [loadEvents]);

  const metricNameById = useMemo(() => {
    const map = new Map<number, AlertRule>();
    rules.forEach((r) => map.set(r.id, r));
    return map;
  }, [rules]);

  const metricOptions = useMemo(
    () => metrics.map((m) => ({ value: m.name, label: m.display ? `${m.display}（${m.name}）` : m.name })),
    [metrics],
  );

  const handleRefresh = async () => {
    setLoading(true);
    try {
      await Promise.all([loadRules(), loadEvents()]);
    } finally {
      setLoading(false);
    }
  };

  const openCreate = () => {
    setEditing(null);
    form.resetFields();
    form.setFieldsValue({
      metric: undefined,
      dimensions: [],
      comparison: 'gt',
      threshold: 0,
      severity: 'notice',
      enabled: true,
    });
    setModalOpen(true);
  };

  const openEdit = (rule: AlertRule) => {
    setEditing(rule);
    form.setFieldsValue({
      id: rule.id,
      metric: rule.metric,
      dimensions: rule.dimensions,
      comparison: rule.comparison,
      threshold: rule.threshold,
      severity: rule.severity,
      enabled: rule.enabled,
    });
    setModalOpen(true);
  };

  const handleDelete = async (rule: AlertRule) => {
    try {
      await deleteAlertRule(rule.id);
      void message.success('规则已删除');
      await Promise.all([loadRules(), loadEvents()]);
    } catch (error: unknown) {
      void message.error(error instanceof Error ? error.message : String(error));
    }
  };

  const handleSubmit = async () => {
    const values = await form.validateFields();
    const body: AlertRuleBody = {
      ...values,
      // deviation 只支持整体口径：提交前强制清空维度，避免后端 400
      dimensions: values.comparison === 'deviation' ? [] : values.dimensions ?? [],
      id: editing?.id ?? null,
    };
    try {
      await upsertAlertRule(body);
      void message.success(editing ? '规则已更新' : '规则已创建');
      setModalOpen(false);
      await loadRules();
    } catch (error: unknown) {
      void message.error(error instanceof Error ? error.message : String(error));
    }
  };

  const handleCheck = async () => {
    setChecking(true);
    try {
      const res = await runAlertCheck();
      const hitCount = res.events.filter((e) => e.hit).length;
      const errCount = res.events.filter((e) => e.status === 'error').length;
      void message.success(
        `已求值 ${res.checked} 条规则（周期 ${res.evaluated_at.replace('T', ' ')}）：命中 ${hitCount}，失败 ${errCount}`,
      );
      await Promise.all([loadRules(), loadEvents()]);
    } catch (error: unknown) {
      void message.error(error instanceof Error ? error.message : String(error));
    } finally {
      setChecking(false);
    }
  };

  const handleSeen = async (event: AlertEvent) => {
    try {
      const res = await markAlertEventSeen(event.id, true);
      setUnread(res.unread);
      await loadEvents();
    } catch (error: unknown) {
      void message.error(error instanceof Error ? error.message : String(error));
    }
  };

  // 一键下钻：命中切片自带问句，回填到首页提问框（ChatPage 读 ?q= 自动发送）
  const drill = (question: string) => {
    navigate(`/?q=${encodeURIComponent(question)}`);
  };

  const selectedMetric = Form.useWatch('metric', form) as string | undefined;
  const selectedComparison = Form.useWatch('comparison', form) as AlertComparison | undefined;
  const dimensionOptions = useMemo(() => {
    const metric = metrics.find((m) => m.name === selectedMetric);
    return (metric?.dimensions ?? []).map((d) => ({ value: d, label: d }));
  }, [metrics, selectedMetric]);

  const ruleColumns = [
    { title: 'ID', dataIndex: 'id', width: 60 },
    {
      title: '指标',
      dataIndex: 'metric',
      render: (name: string) => {
        const doc = metrics.find((m) => m.name === name);
        return doc?.display ?? name;
      },
    },
    {
      title: '维度',
      dataIndex: 'dimensions',
      render: (dims: string[]) => (dims.length ? dims.join('、') : <Text type="secondary">整体</Text>),
    },
    {
      title: '条件',
      key: 'cond',
      render: (_: unknown, rule: AlertRule) => {
        const opt = COMPARISON_OPTIONS.find((c) => c.value === rule.comparison);
        const thresholdText = rule.comparison === 'deviation' ? `${rule.threshold}%` : String(rule.threshold);
        return (
          <Tooltip title={opt?.hint}>
            <Tag>{opt?.label ?? rule.comparison}</Tag>
            <Text>{thresholdText}</Text>
          </Tooltip>
        );
      },
    },
    {
      title: '严重度',
      dataIndex: 'severity',
      width: 90,
      render: (s: AlertSeverity) => (
        <Tag color={s === 'attention' ? 'volcano' : 'default'}>
          {SEVERITY_OPTIONS.find((x) => x.value === s)?.label ?? s}
        </Tag>
      ),
    },
    {
      title: '启用',
      dataIndex: 'enabled',
      width: 70,
      render: (enabled: boolean) => <Tag color={enabled ? 'green' : 'default'}>{enabled ? '启用' : '停用'}</Tag>,
    },
    {
      title: '操作',
      key: 'ops',
      width: 120,
      render: (_: unknown, rule: AlertRule) => (
        <Space size={4}>
          <Button size="small" type="text" icon={<EditOutlined />} onClick={() => openEdit(rule)}>
            编辑
          </Button>
          <Popconfirm
            title="删除该规则？其历史事件仍保留"
            onConfirm={() => handleDelete(rule)}
          >
            <Button size="small" type="text" danger icon={<DeleteOutlined />}>
              删除
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  const eventColumns = [
    { title: '事件 ID', dataIndex: 'id', width: 80 },
    {
      title: '状态',
      dataIndex: 'status',
      width: 90,
      render: (status: string, event: AlertEvent) => {
        if (status === 'error') return <Tag color="error">求值失败</Tag>;
        if (event.hit) return <Tag color="warning">命中</Tag>;
        return <Tag>未命中</Tag>;
      },
    },
    {
      title: '严重度',
      dataIndex: 'severity',
      width: 80,
      render: (s: AlertSeverity | null) =>
        s === null ? (
          <Text type="secondary">-</Text>
        ) : (
          <Tag color={s === 'attention' ? 'volcano' : 'default'}>
            {SEVERITY_OPTIONS.find((x) => x.value === s)?.label ?? s}
          </Tag>
        ),
    },
    {
      title: '规则',
      key: 'rule',
      render: (_: unknown, event: AlertEvent) => {
        const rule = metricNameById.get(event.rule_id);
        return <Text>{rule ? `${rule.metric}（#${rule.id}）` : `#${event.rule_id}（已删除）`}</Text>;
      },
    },
    {
      title: '求值周期',
      dataIndex: 'evaluated_at',
      width: 170,
      render: (v: string) => v.replace('T', ' ').slice(0, 16),
    },
    {
      title: '说明 / 命中切片',
      key: 'reason',
      render: (_: unknown, event: AlertEvent) => {
        if (event.status === 'error') {
          return <Text type="danger">{event.reason || '求值失败'}</Text>;
        }
        if (!event.hits.length) return <Text type="secondary">{event.reason || '未命中'}</Text>;
        return (
          <Space direction="vertical" size={4}>
            {event.hits.map((hit, i) => (
              <Space key={i} size={8}>
                <Tag color="orange">{formatSlice(event, i)}</Tag>
                <Text style={{ fontSize: 12 }}>{hit.reason}</Text>
                <Button size="small" type="link" onClick={() => drill(hit.question)}>
                  去对话里下钻
                </Button>
              </Space>
            ))}
          </Space>
        );
      },
    },
    {
      title: '已读',
      dataIndex: 'seen',
      width: 90,
      render: (seen: boolean, event: AlertEvent) =>
        seen ? (
          <Tag>已读</Tag>
        ) : (
          <Button size="small" onClick={() => handleSeen(event)}>
            标为已读
          </Button>
        ),
    },
  ];

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12, height: '100%', minHeight: 0 }}>
      <Card size="small" style={{ borderRadius: 8 }} styles={{ body: { padding: '10px 12px' } }}>
        <Space style={{ width: '100%', justifyContent: 'space-between' }}>
          <Space size={8}>
            <Text strong style={{ fontSize: 15 }}>
              异常订阅
            </Text>
            <Tooltip title="命中 / 失败的未读事件数，顶栏角标与此同源（/api/health 的 alert_unread）">
              <Badge count={unread} showZero color={unread > 0 ? '#faad14' : '#bfbfbf'} />
            </Tooltip>
          </Space>
          <Space>
            <Button icon={<ReloadOutlined />} onClick={handleRefresh} loading={loading}>
              刷新
            </Button>
            <Tooltip title="对全部启用规则立即求值一次。同一周期重复调用幂等；周期调度应由外部计划任务调用本接口。">
              <Button type="primary" icon={<ExperimentOutlined />} loading={checking} onClick={handleCheck}>
                立即求值
              </Button>
            </Tooltip>
          </Space>
        </Space>
      </Card>

      <Card size="small" style={{ borderRadius: 8, flex: 1, minHeight: 0, overflow: 'auto' }}>
        <Tabs
          defaultActiveKey="rules"
          items={[
            {
              key: 'rules',
              label: `规则（${rules.length}）`,
              children: (
                <>
                  <Space style={{ marginBottom: 12 }}>
                    <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
                      新建规则
                    </Button>
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      订阅指标越界 / 偏离，求值复用问答链路合成问句，同周期复检幂等
                    </Text>
                  </Space>
                  <Table
                    rowKey="id"
                    size="small"
                    columns={ruleColumns}
                    dataSource={rules}
                    pagination={false}
                  />
                </>
              ),
            },
            {
              key: 'events',
              label: (
                <span>
                  事件{unread > 0 ? <Badge count={unread} size="small" color="#faad14" /> : null}
                </span>
              ),
              children: (
                <>
                  <Space style={{ marginBottom: 12 }}>
                    <Radio.Group
                      value={seenFilter === undefined ? 'all' : seenFilter ? 'seen' : 'unseen'}
                      onChange={(e) => {
                        const v = e.target.value as string;
                        setSeenFilter(v === 'all' ? undefined : v === 'seen');
                      }}
                      optionType="button"
                      buttonStyle="solid"
                      options={[
                        { value: 'all', label: '全部' },
                        { value: 'unseen', label: '未读' },
                        { value: 'seen', label: '已读' },
                      ]}
                    />
                    <Select
                      allowClear
                      placeholder="按规则筛选"
                      style={{ width: 240 }}
                      value={ruleFilter}
                      onChange={(v) => setRuleFilter(v)}
                      options={rules.map((r) => ({ value: r.id, label: `#${r.id} ${r.metric}` }))}
                    />
                  </Space>
                  <Table
                    rowKey="id"
                    size="small"
                    columns={eventColumns}
                    dataSource={events}
                    pagination={{ pageSize: 20, showSizeChanger: false }}
                  />
                </>
              ),
            },
          ]}
        />
      </Card>

      <Modal
        title={editing ? `编辑规则 #${editing.id}` : '新建订阅规则'}
        open={modalOpen}
        onCancel={() => setModalOpen(false)}
        onOk={handleSubmit}
        okText="保存"
        cancelText="取消"
        destroyOnClose
      >
        <Form form={form} layout="vertical" style={{ marginTop: 12 }}>
          <Form.Item
            name="metric"
            label="指标"
            rules={[{ required: true, message: '请选择语义层已登记的指标' }]}
          >
            <Select
              showSearch
              optionFilterProp="label"
              options={metricOptions}
              placeholder="选择要订阅的指标"
            />
          </Form.Item>
          <Form.Item name="dimensions" label="分组维度（不选表示整体口径）">
            <Select
              mode="multiple"
              options={dimensionOptions}
              placeholder="选择分组维度，可多选"
              disabled={!selectedMetric || selectedComparison === 'deviation'}
            />
          </Form.Item>
          <Form.Item name="comparison" label="比较方式" rules={[{ required: true }]}>
            <Radio.Group options={COMPARISON_OPTIONS.map((c) => ({ value: c.value, label: c.label }))} />
          </Form.Item>
          <Form.Item
            name="threshold"
            label={selectedComparison === 'deviation' ? '偏离阈值（%，如 5 表示 5%）' : '阈值'}
            rules={[{ required: true, message: '请输入阈值' }]}
          >
            <InputNumber style={{ width: '100%' }} step={selectedComparison === 'deviation' ? 1 : 0.01} />
          </Form.Item>
          <Form.Item name="severity" label="严重度" rules={[{ required: true }]}>
            <Radio.Group options={SEVERITY_OPTIONS} />
          </Form.Item>
          <Form.Item name="enabled" label="启用" valuePropName="checked">
            <Switch checkedChildren="启用" unCheckedChildren="停用" />
          </Form.Item>
          {selectedComparison === 'deviation' && (
            <Paragraph type="secondary" style={{ fontSize: 12 }}>
              偏离度订阅暂不支持按维度切片，维度选择已停用。
            </Paragraph>
          )}
        </Form>
      </Modal>
    </div>
  );
}
