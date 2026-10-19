import { useCallback, useEffect, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import {
  Alert,
  Button,
  Card,
  Collapse,
  DatePicker,
  Empty,
  Form,
  InputNumber,
  Modal,
  Select,
  Space,
  Tooltip,
  Typography,
  message,
} from 'antd';
import { DeleteOutlined, PlusOutlined } from '@ant-design/icons';
import dayjs, { type Dayjs } from 'dayjs';
import {
  fetchFilterFields,
  previewFormQuery,
  submitFormQuery,
  type FilterField,
  type FormOp,
  type FormPreview,
  type FormQueryBody,
} from '@/api/formQuery';
import { fetchDimensions, fetchMetrics, type DimensionDoc, type MetricDoc } from '@/api/semantic';
import { createSession } from '@/api/session';
import { ResultTable } from '@/components/ResultTable';
import type { ChatResult } from '@/api/types';

const { Text, Paragraph } = Typography;
const { RangePicker } = DatePicker;

const OP_LABELS: Record<FormOp, string> = {
  eq: '等于',
  in: '属于（多值）',
  between: '区间（左闭右开）',
  gte: '不早于',
  lte: '不晚于',
};

interface ConditionDraft {
  field?: string;
  op?: FormOp;
  /** enum 字段为取值或取值数组；日期字段为 YYYY-MM-DD 字符串 */
  value?: string | string[] | null;
  value_to?: string | null;
}

interface FormValues {
  metric?: string;
  dimensions?: string[];
  range?: [Dayjs, Dayjs] | null;
  top_n?: number | null;
  conditions?: ConditionDraft[];
}

function toBody(values: FormValues): FormQueryBody {
  const conditions = (values.conditions ?? [])
    .filter((c) => c.field && c.op)
    .map((c) => ({
      field: c.field as string,
      op: c.op as FormOp,
      value: c.value ?? null,
      value_to: c.value_to ?? null,
    }));
  return {
    metric: values.metric ?? '',
    dimensions: values.dimensions ?? [],
    conditions,
    time_range: values.range
      ? { start: values.range[0].format('YYYY-MM-DD'), end: values.range[1].format('YYYY-MM-DD') }
      : null,
    top_n: values.top_n ?? null,
  };
}

/**
 * 复杂查询表单页（FR-P-1 ~ FR-P-5）。
 *
 * 与自由提问的分界线：这里选的指标、维度与条件都是确定的，后端不再让模型从
 * 问句里抽一遍，只由模型决定「怎么写」。因此界面的职责是把确定的东西表达清楚
 * ——字段、算子、取值全部来自语义层，并在执行前把条件渲染成谓词给人确认。
 */
interface Props {
  /** 一轮查询落库后回调，用于让侧边栏刷新列表（标题 / 轮次此时才定下来） */
  onTurnComplete?: () => void;
}

export function FormQueryPage({ onTurnComplete }: Props) {
  const [params] = useSearchParams();
  const [form] = Form.useForm<FormValues>();
  const [metrics, setMetrics] = useState<MetricDoc[]>([]);
  const [dimensions, setDimensions] = useState<DimensionDoc[]>([]);
  const [fields, setFields] = useState<FilterField[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [preview, setPreview] = useState<FormPreview | null>(null);
  const [result, setResult] = useState<ChatResult | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [previewing, setPreviewing] = useState(false);

  useEffect(() => {
    Promise.all([
      fetchMetrics(),
      fetchDimensions(),
      fetchFilterFields(),
    ])
      .then(([m, d, f]) => {
        setMetrics(m);
        setDimensions(d);
        setFields(f);
      })
      .catch((error: unknown) => {
        setLoadError(error instanceof Error ? error.message : String(error));
      });
  }, []);

  const watched = Form.useWatch([], form) as FormValues | undefined;
  const selectedMetric = watched?.metric;
  const conditions = watched?.conditions ?? [];

  const metricOptions = useMemo(
    () =>
      metrics.map((m) => ({
        value: m.name,
        label: m.display ? `${m.display}（${m.name}）` : m.name,
      })),
    [metrics],
  );

  const metricDef = useMemo(
    () => metrics.find((m) => m.name === selectedMetric) ?? null,
    [metrics, selectedMetric],
  );

  const dimensionOptions = useMemo(() => {
    const allowed = new Set(metricDef?.dimensions ?? []);
    return dimensions.map((d) => {
      const supported = allowed.size === 0 || allowed.has(d.name);
      return {
        value: d.name,
        label: d.display || d.name,
        disabled: !supported,
        // 置灰而不是隐藏：只列支持的维度会让人以为其它维度不存在
        reason: supported ? undefined : `指标「${metricDef?.display ?? selectedMetric}」不支持按此维度分组`,
      };
    });
  }, [dimensions, metricDef, selectedMetric]);

  const usedFields = useMemo(
    () => new Set(conditions.map((c) => c?.field).filter((f): f is string => Boolean(f))),
    [conditions],
  );

  const fieldOptions = useMemo(
    () =>
      fields.map((f) => {
        const conflict = (f.exclude_with ?? []).find((other) => usedFields.has(other));
        const other = fields.find((x) => x.field === conflict);
        // 顶部时间区间与条件行里的下单时间是同一个字段，同时设会互相矛盾
        const duplicatedTime = f.field === 'orders.created_at' && Boolean(watched?.range);
        return {
          value: f.field,
          label: f.display || f.field,
          disabled: Boolean(conflict) || duplicatedTime,
          reason: conflict
            ? `与已选条件「${other?.display ?? conflict}」互斥，同时使用结果恒为空集`
            : duplicatedTime
              ? '已选顶部时间区间，同一字段不可重复设置'
              : undefined,
          hint: f.note,
        };
      }),
    [fields, usedFields, watched?.range],
  );

  const findField = useCallback(
    (name?: string) => fields.find((f) => f.field === name) ?? null,
    [fields],
  );

  const showError = (error: unknown) => {
    void message.error(error instanceof Error ? error.message : String(error));
  };

  const handlePreview = async () => {
    const values = await form.validateFields().catch(() => null);
    if (!values) return;
    setPreviewing(true);
    try {
      const res = await previewFormQuery(toBody(values));
      setPreview(res);
      return res;
    } catch (error: unknown) {
      showError(error);
      return null;
    } finally {
      setPreviewing(false);
    }
  };

  const runSubmit = async (body: FormQueryBody) => {
    setSubmitting(true);
    try {
      // 与对话页同一套惰性创建：地址栏带了会话就用它，没有才新建
      const sid = params.get('session') ?? (await createSession()).id;
      const res = await submitFormQuery(sid, body);
      setResult(res);
      onTurnComplete?.();
    } catch (error: unknown) {
      showError(error);
    } finally {
      setSubmitting(false);
    }
  };

  const handleSubmit = async () => {
    const values = await form.validateFields().catch(() => null);
    if (!values) return;
    const body = toBody(values);
    // P5：执行前把条件渲染成谓词给人确认，而不是执行失败后再解释
    let res: FormPreview | null = null;
    try {
      res = await previewFormQuery(body);
      setPreview(res);
    } catch (error: unknown) {
      showError(error);
      return;
    }
    Modal.confirm({
      title: '确认执行这次查询',
      width: 560,
      okText: '执行',
      cancelText: '再改改',
      content: (
        <div style={{ fontSize: 13 }}>
          <Paragraph style={{ marginBottom: 8 }}>
            <Text type="secondary">渲染问句：</Text>
            {res.question}
          </Paragraph>
          <Text type="secondary">以下条件会逐条落入 SQL：</Text>
          <ul style={{ margin: '6px 0 0', paddingLeft: 18 }}>
            {res.predicates.map((p, i) => (
              <li key={i}>
                <Text code>{p}</Text>
              </li>
            ))}
          </ul>
        </div>
      ),
      onOk: () => runSubmit(body),
    });
  };

  const disabledConditions = !selectedMetric;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12, height: '100%', minHeight: 0, overflow: 'auto' }}>
      <Card size="small" style={{ borderRadius: 8 }} styles={{ body: { padding: '12px 14px' } }}>
        <Space direction="vertical" size={4} style={{ width: '100%' }}>
          <Text strong style={{ fontSize: 15 }}>
            复杂查询表单
          </Text>
          <Text type="secondary" style={{ fontSize: 12 }}>
            指标、维度与条件由表单直接给出，不经自然语言转述：字段、算子与取值全部取自语义层白名单，
            执行前渲染成谓词供确认。时间区间按左闭右开处理，与语义层的时间口径一致。
          </Text>
        </Space>
      </Card>

      {loadError && <Alert type="error" showIcon message={loadError} />}

      <Card size="small" style={{ borderRadius: 8 }}>
        <Form
          form={form}
          layout="vertical"
          initialValues={{ metric: undefined, dimensions: [], conditions: [], top_n: null }}
          requiredMark={false}
        >
          <Space size={16} wrap style={{ width: '100%' }}>
            <Form.Item
              name="metric"
              label="指标"
              style={{ minWidth: 240, marginBottom: 8 }}
              rules={[{ required: true, message: '请选择语义层已登记的指标' }]}
            >
              <Select
                showSearch
                optionFilterProp="label"
                placeholder="选择指标"
                options={metricOptions}
                style={{ width: 240 }}
              />
            </Form.Item>
            <Form.Item name="dimensions" label="分组维度" style={{ minWidth: 320, marginBottom: 8 }}>
              <Select
                mode="multiple"
                allowClear
                placeholder="不选表示看整体，可多选"
                disabled={!selectedMetric}
                options={dimensionOptions.map((o) => ({
                  value: o.value,
                  label: o.reason ? (
                    <Tooltip title={o.reason}>
                      <span style={{ color: 'rgba(0,0,0,0.25)' }}>{o.label}</span>
                    </Tooltip>
                  ) : (
                    o.label
                  ),
                  disabled: o.disabled,
                }))}
                style={{ minWidth: 320 }}
              />
            </Form.Item>
            <Form.Item name="range" label="下单时间区间" style={{ marginBottom: 8 }}>
              <RangePicker placeholder={['开始', '结束']} />
            </Form.Item>
            <Form.Item name="top_n" label="取前 N 条" style={{ marginBottom: 8 }}>
              <InputNumber min={1} max={1000} placeholder="不限" style={{ width: 120 }} />
            </Form.Item>
          </Space>

          <div style={{ borderTop: '1px solid var(--wb-border)', paddingTop: 10 }}>
            <Space align="center" style={{ marginBottom: 8 }}>
              <Text strong>过滤条件</Text>
              <Text type="secondary" style={{ fontSize: 12 }}>
                {disabledConditions ? '先选指标再添加条件' : '行与行之间为「且」的关系'}
              </Text>
            </Space>

            <Form.List name="conditions">
              {(listFields, { add, remove }) => (
                <Space direction="vertical" size={8} style={{ width: '100%' }}>
                  {listFields.map((field) => {
                    const draft = conditions[field.name];
                    const def = findField(draft?.field);
                    const isDate = def?.values_from === 'date';
                    const op = draft?.op;
                    return (
                      <Space key={field.key} align="start" wrap>
                        <Form.Item name={[field.name, 'field']} style={{ marginBottom: 0 }}>
                          <Select
                            placeholder="条件字段"
                            style={{ width: 200 }}
                            disabled={disabledConditions}
                            options={fieldOptions.map((o) => ({
                              value: o.value,
                              label: o.reason ? (
                                <Tooltip title={o.reason}>
                                  <span style={{ color: 'rgba(0,0,0,0.25)' }}>{o.label}</span>
                                </Tooltip>
                              ) : (
                                o.label
                              ),
                              disabled: o.disabled,
                            }))}
                            onChange={() => {
                              // 换字段后算子与取值都不再适用，一并清空
                              form.setFieldValue(['conditions', field.name, 'op'], undefined);
                              form.setFieldValue(['conditions', field.name, 'value'], undefined);
                              form.setFieldValue(['conditions', field.name, 'value_to'], undefined);
                            }}
                          />
                        </Form.Item>
                        <Form.Item name={[field.name, 'op']} style={{ marginBottom: 0 }}>
                          <Select
                            placeholder="算子"
                            style={{ width: 150 }}
                            disabled={!def}
                            options={(def?.ops ?? []).map((o) => ({ value: o, label: OP_LABELS[o] }))}
                            onChange={() => {
                              form.setFieldValue(['conditions', field.name, 'value'], undefined);
                              form.setFieldValue(['conditions', field.name, 'value_to'], undefined);
                            }}
                          />
                        </Form.Item>
                        {isDate ? (
                          op === 'between' ? (
                            // 日期值存在同一行的 value / value_to 上，不另起字段：
                            // 再包一层 Form.Item 会让它与这里的 value 互相覆盖
                            <RangePicker
                              placeholder={['开始', '结束']}
                              value={
                                draft?.value && draft?.value_to
                                  ? [dayjs(String(draft.value)), dayjs(String(draft.value_to))]
                                  : null
                              }
                              onChange={(dates) => {
                                form.setFieldValue(
                                  ['conditions', field.name, 'value'],
                                  dates?.[0]?.format('YYYY-MM-DD') ?? undefined,
                                );
                                form.setFieldValue(
                                  ['conditions', field.name, 'value_to'],
                                  dates?.[1]?.format('YYYY-MM-DD') ?? undefined,
                                );
                              }}
                            />
                          ) : (
                            <DatePicker
                              placeholder="选择日期"
                              disabled={!op}
                              value={draft?.value ? dayjs(String(draft.value)) : null}
                              onChange={(date) => {
                                form.setFieldValue(
                                  ['conditions', field.name, 'value'],
                                  date?.format('YYYY-MM-DD') ?? undefined,
                                );
                              }}
                            />
                          )
                        ) : (
                          <Form.Item name={[field.name, 'value']} style={{ marginBottom: 0 }}>
                            <Select
                              placeholder={op === 'in' ? '可多选' : '选择取值'}
                              style={{ minWidth: 200 }}
                              disabled={!op}
                              mode={op === 'in' ? 'multiple' : undefined}
                              options={(def?.values ?? []).map((v) => ({
                                value: v.value,
                                label: v.label,
                              }))}
                            />
                          </Form.Item>
                        )}
                        {def?.note && (
                          <Tooltip title={def.note}>
                            <Text type="secondary" style={{ fontSize: 12, maxWidth: 260, lineHeight: '32px' }}>
                              有使用约束
                            </Text>
                          </Tooltip>
                        )}
                        <Button
                          type="text"
                          danger
                          icon={<DeleteOutlined />}
                          onClick={() => remove(field.name)}
                        />
                      </Space>
                    );
                  })}
                  <Button
                    type="dashed"
                    icon={<PlusOutlined />}
                    disabled={disabledConditions}
                    onClick={() => add({})}
                    style={{ width: 200 }}
                  >
                    添加条件
                  </Button>
                </Space>
              )}
            </Form.List>
          </div>

          <Space style={{ marginTop: 14 }}>
            <Button loading={previewing} onClick={() => void handlePreview()}>
              预演
            </Button>
            <Button type="primary" loading={submitting} onClick={() => void handleSubmit()}>
              执行查询
            </Button>
          </Space>
        </Form>
      </Card>

      {preview && (
        <Card size="small" title="提交前回显" style={{ borderRadius: 8 }}>
          <Space direction="vertical" size={6} style={{ width: '100%' }}>
            <Text>
              <Text type="secondary">渲染问句：</Text>
              {preview.question}
            </Text>
            {preview.predicates.length === 0 ? (
              <Text type="secondary" style={{ fontSize: 12 }}>
                未设置条件，按指标与维度直接查询。
              </Text>
            ) : (
              <ul style={{ margin: 0, paddingLeft: 18 }}>
                {preview.predicates.map((p, i) => (
                  <li key={i}>
                    <Text code>{p}</Text>
                    <Text type="secondary" style={{ fontSize: 12, marginLeft: 8 }}>
                      {preview.descriptions[i]}
                    </Text>
                  </li>
                ))}
              </ul>
            )}
          </Space>
        </Card>
      )}

      <Card size="small" title="查询结果" style={{ borderRadius: 8 }}>
        {!result ? (
          <Empty description="填写表单后执行查询" image={Empty.PRESENTED_IMAGE_SIMPLE} />
        ) : (
          <Space direction="vertical" size={10} style={{ width: '100%' }}>
            {result.status !== 'ok' && (
              <Alert type="warning" showIcon message={`本轮未取到数据（${result.status}）`} description={result.sql_error || result.answer} />
            )}
            {result.original_question && (
              <Text type="secondary" style={{ fontSize: 12 }}>
                落库问句：{result.original_question}
              </Text>
            )}
            {result.answer && <Paragraph style={{ marginBottom: 0 }}>{result.answer}</Paragraph>}
            {result.insight && (
              <Paragraph type="secondary" style={{ marginBottom: 0, fontSize: 13 }}>
                {result.insight}
              </Paragraph>
            )}
            {result.sql && (
              <Collapse
                size="small"
                items={[
                  {
                    key: 'sql',
                    label: '执行的 SQL',
                    children: (
                      <pre style={{ margin: 0, whiteSpace: 'pre-wrap', fontSize: 12 }}>{result.sql}</pre>
                    ),
                  },
                ]}
              />
            )}
            {(result.slot_gaps?.length ?? 0) > 0 && (
              <Alert
                type="info"
                showIcon
                message="条件覆盖核对：以下表单条件未出现在语句里"
                description={
                  <ul style={{ margin: 0, paddingLeft: 18 }}>
                    {(result.slot_gaps ?? []).map((gap, i) => (
                      <li key={i}>
                        <Text>{gap.expected}</Text>
                        <Text type="secondary" style={{ fontSize: 12 }}> — {gap.detail}</Text>
                      </li>
                    ))}
                  </ul>
                }
              />
            )}
            {result.columns.length > 0 ? (
              <ResultTable
                columns={result.columns}
                rows={result.rows}
                truncated={result.truncated}
                totalRowCount={result.row_count}
                exportPrefix="form-query"
              />
            ) : (
              <Empty description="结果集为空" image={Empty.PRESENTED_IMAGE_SIMPLE} />
            )}
          </Space>
        )}
      </Card>
    </div>
  );
}
