import { useState } from 'react';
import { Button, Dropdown, Input, Modal, Space, Typography, message } from 'antd';
import { ExportOutlined } from '@ant-design/icons';
import type { MenuProps } from 'antd';
import type { ChatTurn } from '@/hooks/useChatStream';
import { buildCsv, csvFilename, downloadText, localTimestamp } from '@/lib/exporters';
import { createShare, buildShareUrl } from '@/api/share';

const { Text, Paragraph } = Typography;

interface Props {
  turn: ChatTurn;
  sessionId: string;
}

/**
 * 导出菜单（FR-N-1 / FR-N-3 / FR-N-4）。
 *
 *   PDF          整页导出：用浏览器原生打印 + 临时注入 @media print 样式，
 *                隐藏顶栏与侧栏，只保留本轮卡片。页脚注入「数据截至 + 生成时间」。
 *   CSV          复用 ResultTable 的导出，直接给一份单轮 CSV。
 *   分享链接      调用 /api/share 生成快照与 token，前端路由 /#/share/{token}。
 *
 * FR-N-4：PDF 页脚与 PPTX 首页都要带「数据截至 + 生成时间」。数据截至取
 * data_freshness.orders_cutoff 或 refunds_cutoff，都没有时回退到轮次创建时间。
 *
 * 不在本组件内做的事：
 *   - PPTX 导出（FR-N-2）：按周会模板的整轮 PPTX 在 HistoryPage 里做，
 *     ExportMenu 只承担单轮的导出与分享。
 */
export function ExportMenu({ turn, sessionId }: Props) {
  const [shareOpen, setShareOpen] = useState(false);
  const [shareUrl, setShareUrl] = useState('');
  const [creating, setCreating] = useState(false);

  const result = turn.result;
  if (!result) return null;

  const dataFreshness = result.data_freshness;
  const dataCutoff =
    dataFreshness?.orders_cutoff ||
    dataFreshness?.refunds_cutoff ||
    turn.createdAt ||
    null;
  const generatedAt = localTimestamp();
  // 紧凑的页脚文案，PDF 与 PPTX 共用
  const footerText = `数据截至：${dataCutoff ?? '未知'}　生成时间：${generatedAt}`;

  const onPrintPdf = () => {
    // FR-N-1 整页导出 PDF：注入临时 @media print 样式，隐藏顶栏与侧栏，
    // 在 body 末尾追加一个页脚 div，触发 window.print() 后清理。
    const styleId = 't2sql-print-style';
    const footerId = 't2sql-print-footer';
    document.getElementById(styleId)?.remove();
    document.getElementById(footerId)?.remove();

    const style = document.createElement('style');
    style.id = styleId;
    // 隐藏顶栏、输入区、操作按钮、技术细节折叠面板等只对交互有意义的内容。
    // 整页 PDF 必须含问句、结论、口径与数据时效、异常标记、图表、明细表。
    style.textContent = `
      @media print {
        header, nav, aside, footer { display: none !important; }
        .ant-layout-header, .ant-layout-sider { display: none !important; }
        .t2sql-no-print { display: none !important; }
        .ant-collapse-item:not(.t2sql-print-expand) .ant-collapse-content {
          display: block !important; height: auto !important;
        }
        body { background: #fff !important; }
        #${footerId} {
          position: fixed; bottom: 0; left: 0; right: 0;
          padding: 6px 12px; font-size: 11px; color: #666;
          border-top: 1px solid #eee; text-align: center;
        }
        @page { margin: 16mm 12mm 20mm 12mm; }
      }
    `;
    document.head.appendChild(style);

    const footer = document.createElement('div');
    footer.id = footerId;
    footer.textContent = footerText;
    document.body.appendChild(footer);

    const cleanup = () => {
      style.remove();
      footer.remove();
      window.removeEventListener('afterprint', cleanup);
    };
    window.addEventListener('afterprint', cleanup);
    // 兜底：某些浏览器不会触发 afterprint
    setTimeout(cleanup, 60000);

    window.print();
    void message.info('已打开打印对话框，选择「另存为 PDF」即可导出');
  };

  const onExportCsv = () => {
    const columns = result.columns ?? [];
    const rows = result.rows ?? [];
    if (!columns.length || !rows.length) {
      void message.warning('本轮没有可导出的明细行');
      return;
    }
    const csv = buildCsv(columns, rows.slice(0, 1000), {
      truncated: Boolean(result.truncated),
      totalRowCount: result.row_count,
      limit: 1000,
    });
    downloadText(
      csvFilename(`turn_${turn.turnId ?? 'current'}`, Boolean(result.truncated)),
      csv,
      'text/csv;charset=utf-8',
    );
  };

  const onCreateShare = async () => {
    if (!turn.turnId) {
      void message.error('本轮尚未落库，无法生成分享链接');
      return;
    }
    setCreating(true);
    try {
      const res = await createShare({ turn_id: turn.turnId, session_id: sessionId });
      const url = buildShareUrl(res.token);
      setShareUrl(url);
      void message.success('已生成分享链接');
    } catch (err) {
      void message.error(err instanceof Error ? err.message : String(err));
    } finally {
      setCreating(false);
    }
  };

  const onCopyShare = async () => {
    try {
      await navigator.clipboard?.writeText(shareUrl);
      void message.success('已复制到剪贴板');
    } catch {
      void message.warning('复制失败，请手动复制链接');
    }
  };

  const items: MenuProps['items'] = [
    { key: 'pdf', label: '导出 PDF（打印）', onClick: onPrintPdf },
    { key: 'csv', label: '导出 CSV（明细）', onClick: onExportCsv },
    { type: 'divider' },
    {
      key: 'share',
      label: '生成只读分享链接',
      onClick: () => {
        setShareUrl('');
        setShareOpen(true);
      },
    },
  ];

  return (
    <>
      <Dropdown menu={{ items }} trigger={['click']}>
        <Button size="small" icon={<ExportOutlined />} className="t2sql-no-print">
          导出 / 分享
        </Button>
      </Dropdown>

      <Modal
        title="只读分享链接"
        open={shareOpen}
        onCancel={() => setShareOpen(false)}
        footer={[
          <Button key="close" onClick={() => setShareOpen(false)}>
            关闭
          </Button>,
          shareUrl ? (
            <Button key="copy" type="primary" onClick={onCopyShare}>
              复制链接
            </Button>
          ) : null,
        ]}
      >
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Text type="secondary" style={{ fontSize: 12 }}>
            链接即凭据：拿到链接的人都能查看本轮快照，不需要登录。快照内容只读，
            不含聊天上下文，不可用于提交新问题。FR-I-3 数据掩码（如有）将在第 7 批接入。
          </Text>
          {shareUrl ? (
            <Input.Group compact>
              <Input
                value={shareUrl}
                readOnly
                style={{ width: 'calc(100% - 90px)' }}
                onClick={(e) => e.currentTarget.select()}
              />
              <Button type="primary" style={{ width: 90 }} onClick={onCopyShare}>
                复制
              </Button>
            </Input.Group>
          ) : (
            <Space>
              <Button type="primary" loading={creating} onClick={onCreateShare}>
                生成链接
              </Button>
              {!turn.turnId && (
                <Text type="warning" style={{ fontSize: 12 }}>
                  本轮尚未落库，无法生成分享
                </Text>
              )}
            </Space>
          )}
          <Paragraph type="secondary" style={{ fontSize: 11, margin: 0 }}>
            {footerText}
          </Paragraph>
        </Space>
      </Modal>
    </>
  );
}
