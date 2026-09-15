import { Table, Tag, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';

const { Text } = Typography;

interface Props {
  columns: string[];
  rows: Record<string, unknown>[];
  /** 分页阈值。超过则分页，否则一次性呈现 */
  pageSize?: number;
}

/** 单元格渲染：null 用占位符，数字靠右并加千分位，布尔与对象转成文本。 */
function renderCell(value: unknown) {
  if (value === null || value === undefined || value === '') {
    return <Text type="secondary">—</Text>;
  }
  if (typeof value === 'number') {
    return (
      <span style={{ display: 'block', textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>
        {Number.isInteger(value) ? value.toLocaleString('zh-CN') : value.toLocaleString('zh-CN', {
          minimumFractionDigits: 2,
          maximumFractionDigits: 2,
        })}
      </span>
    );
  }
  if (typeof value === 'boolean') return <Tag color={value ? 'green' : 'default'}>{String(value)}</Tag>;
  if (typeof value === 'object') return <Text code>{JSON.stringify(value)}</Text>;
  return <>{String(value)}</>;
}

/**
 * 结果集表格。列名直接取结果集的列名，不做映射 —— 前端不掌握字段语义，
 * 改名会让界面与 SQL 对不上。
 */
export function ResultTable({ columns, rows, pageSize = 50 }: Props) {
  if (!columns.length) return null;

  const tableColumns: ColumnsType<Record<string, unknown>> = columns.map((name) => ({
    title: name,
    dataIndex: name,
    key: name,
    ellipsis: true,
    render: renderCell,
  }));

  return (
    <Table<Record<string, unknown>>
      size="small"
      columns={tableColumns}
      dataSource={rows.map((row, index) => ({ ...row, __key: index }))}
      rowKey="__key"
      scroll={{ x: 'max-content' }}
      pagination={rows.length > pageSize ? { pageSize, showSizeChanger: false, size: 'small' } : false}
    />
  );
}
