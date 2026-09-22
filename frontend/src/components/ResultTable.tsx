import { Button, Space, Table, Tag, Tooltip, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { buildCsv, csvFilename, downloadText } from '@/lib/exporters';

const { Text } = Typography;

interface Props {
  columns: string[];
  rows: Record<string, unknown>[];
  /** 分页阈值。超过则分页，否则一次性呈现 */
  pageSize?: number;
  /** 导出文件名前缀（默认 query） */
  exportPrefix?: string;
  /**
   * 提供时，非数值列的单元格可点击发起下钻（FR-L-2）。
   * 数值列不接下钻：点一个数字判断不出它属于哪个切片，拼出的问句会指向错误的
   * 维度值。维度名取列标题本身，与 findings 的 dimension 同源（都是 SQL 的 AS）。
   */
  onCellDrill?: (payload: { dimension: string; value: string }) => void;
  /** FR-B-1：结果是否被行数上限截断 */
  truncated?: boolean;
  /** FR-B-1：真实命中行数。截断时 rows 只是其中一部分 */
  totalRowCount?: number;
  /** FR-B-1：服务端行数上限，写进导出文件的首行注释 */
  rowLimit?: number;
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
export function ResultTable({
  columns,
  rows,
  pageSize = 50,
  exportPrefix = 'query',
  onCellDrill,
  truncated = false,
  totalRowCount,
  rowLimit,
}: Props) {
  if (!columns.length) return null;

  // 逐列判断是否数值列：看该列第一个非空值。整列为空时按非数值处理，
  // 空列本来也没有可下钻的取值
  const numericColumns = new Set(
    columns.filter((_, index) => {
      for (const row of rows) {
        const value = Object.values(row)[index];
        if (value === null || value === undefined || value === '') continue;
        return typeof value === 'number';
      }
      return false;
    }),
  );

  const tableColumns: ColumnsType<Record<string, unknown>> = columns.map((name, index) => {
    const drillable = Boolean(onCellDrill) && !numericColumns.has(name);
    return {
      title: name,
      // 取值走位置而不是列名。后端已为同名列补过序号后缀，但历史落库的结果集
      // 仍可能是补后缀之前的列名，按列名取会让两列显示同一个值
      dataIndex: ['__cells', index],
      key: `${name}#${index}`,
      ellipsis: true,
      render: (value: unknown) => {
        const cell = renderCell(value);
        if (!drillable || value === null || value === undefined || value === '') return cell;
        return (
          <Tooltip title={`下钻：${String(value)}`}>
            <span
              role="button"
              tabIndex={0}
              style={{ cursor: 'pointer', color: '#1677ff' }}
              onClick={() => onCellDrill?.({ dimension: name, value: String(value) })}
              onKeyDown={(e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                  onCellDrill?.({ dimension: name, value: String(value) });
                }
              }}
            >
              {cell}
            </span>
          </Tooltip>
        );
      },
    };
  });

  const handleExportCsv = () => {
    // 落库的 rows 已经是后端截过 PERSIST_ROWS 的版本，导出等于所见即所得。
    // 这里再截一次 1000 是给一档冗余，避免历史回看出现超大 rows 时下载卡死。
    const trimmed = rows.slice(0, 1000);
    // 截断标记取自服务端的 truncated，而不是在前端比较行数：落库时已经被
    // PERSIST_ROWS 截过一轮，前端拿到的 rows 长度本身说明不了是否还有更多行。
    const csv = buildCsv(columns, trimmed, {
      truncated,
      totalRowCount: totalRowCount ?? rows.length,
      limit: rowLimit ?? 1000,
    });
    downloadText(csvFilename(exportPrefix, truncated), csv, 'text/csv;charset=utf-8');
  };

  return (
    <div>
      <Space style={{ marginBottom: 6 }}>
        <Button size="small" onClick={handleExportCsv} disabled={rows.length === 0}>
          导出 CSV
        </Button>
        <Text type="secondary" style={{ fontSize: 12 }}>
          共 {rows.length} 行
          {truncated && totalRowCount ? `（实际命中 ${totalRowCount} 行，已截断）` : ''}
        </Text>
      </Space>
      <Table<Record<string, unknown>>
        size="small"
        columns={tableColumns}
        dataSource={rows.map((row, index) => ({ __key: index, __cells: Object.values(row) }))}
        rowKey="__key"
        scroll={{ x: 'max-content' }}
        pagination={rows.length > pageSize ? { pageSize, showSizeChanger: false, size: 'small' } : false}
      />
    </div>
  );
}
