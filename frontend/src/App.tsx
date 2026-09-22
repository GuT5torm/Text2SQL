import { useEffect, useState } from 'react';
import { HashRouter, Navigate, Route, Routes } from 'react-router-dom';
import { Badge, Button, ConfigProvider, Layout, Popover, Space, Tag, Tooltip, Typography } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import { AlertCenter } from '@/pages/AlertCenter';
import { ChatPage } from '@/pages/ChatPage';
import { HistoryPage } from '@/pages/HistoryPage';
import { SharePage } from '@/pages/SharePage';
import { fetchHealth } from '@/api/session';
import type { HealthInfo } from '@/api/types';

const { Header, Content } = Layout;
const { Text } = Typography;

/**
 * 只读说明（FR-I-1）：一个常驻的徽标，点击展开详情。
 *
 * 「只读」是本系统的能力而非提示——之前只在被拦截时弹黄条，常驻说明解决
 * 「为什么只能问不能改」的疑问。徽标位于顶栏左侧，紧邻项目名，避免被业务
 * 信息淹没。
 */
function ReadOnlyBadge() {
  const items: { label: string; text: string }[] = [
    { label: '连接', text: '业务库以只读模式连接，所有写入操作都会被沙箱拒绝' },
    { label: '语句', text: '生成的查询限定为 SELECT，且必须带行数上限与时间边界' },
    { label: '字段', text: '字段访问以语义层登记的表与字段为白名单，越权访问被拦截' },
    { label: '结果', text: '返回结果只含聚合数据，不暴露个人身份字段' },
  ];
  return (
    <Popover
      placement="bottomLeft"
      trigger="click"
      content={
        <div style={{ maxWidth: 320, fontSize: 12, lineHeight: 1.7 }}>
          <Text strong style={{ fontSize: 13 }}>系统只读说明</Text>
          <ul style={{ margin: '8px 0 0', paddingLeft: 16 }}>
            {items.map((it) => (
              <li key={it.label}>
                <Text strong>{it.label}：</Text>
                <Text type="secondary">{it.text}</Text>
              </li>
            ))}
          </ul>
        </div>
      }
    >
      <Tag color="default" style={{ cursor: 'help', borderRadius: 4 }}>
        只读
      </Tag>
    </Popover>
  );
}

function Nav({ alertUnread }: { alertUnread: number }) {
  // 用一个最简单的 pathname 判断标识当前路由，避免引入 react-router 的 hooks
  // 让 Shell 组件的可读性下降。历史页由 HistoryPage 自身渲染顶栏态。
  return (
    <Space size={4}>
      <Button type="text" href="#/">
        对话
      </Button>
      <Button type="text" href="#/history">
        历史会话
      </Button>
      <Badge count={alertUnread} size="small" offset={[-6, 4]}>
        <Button type="text" href="#/alerts">
          告警订阅
        </Button>
      </Badge>
    </Space>
  );
}

function Shell() {
  const [health, setHealth] = useState<HealthInfo | null>(null);

  useEffect(() => {
    let timer: number | undefined;
    const pull = () => {
      fetchHealth()
        .then(setHealth)
        .catch(() => setHealth(null));
    };
    pull();
    // FR-O-4：未读角标需要随事件 / 已读动作刷新，30s 轮询 /api/health 足够；
    // 告警中心页内的操作走自己的即时刷新，不依赖这个轮询。
    timer = window.setInterval(pull, 30_000);
    return () => window.clearInterval(timer);
  }, []);

  return (
    <Layout style={{ height: '100vh', background: '#f5f5f5' }}>
      <Header
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          background: '#fff',
          borderBottom: '1px solid #f0f0f0',
          padding: '0 20px',
          height: 56,
        }}
      >
        <Space size={12}>
          <Text strong style={{ fontSize: 16 }}>
            Text2SQL 电商数据分析 Agent
          </Text>
          <ReadOnlyBadge />
          {health && (
            <Tooltip
              title={
                health.llm_mock
                  ? '未配置 LLM_API_KEY，生成的 SQL 来自固定模板，配置密钥后重启即可切换'
                  : `模型 ${health.model} · 指标 ${health.metrics} 个 · 表 ${health.tables} 张`
              }
            >
              <Tag color={health.llm_mock ? 'orange' : 'green'} style={{ borderRadius: 4 }}>
                {health.llm_mock ? '桩实现模式' : health.model}
              </Tag>
            </Tooltip>
          )}
          {health?.alert_unhealthy && (
            <Tooltip
              title={`异常订阅连续求值失败 ${health.alert_consecutive_failures} 次（阈值 ${health.alert_failures_threshold}），请检查规则与链路`}
            >
              <Tag color="red" style={{ borderRadius: 4 }}>
                告警求值异常
              </Tag>
            </Tooltip>
          )}
        </Space>
        <Nav alertUnread={health?.alert_unread ?? 0} />
      </Header>
      <Content style={{ padding: 16, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
        <Routes>
          <Route path="/" element={<ChatPage />} />
          <Route path="/history" element={<HistoryPage />} />
          <Route path="/alerts" element={<AlertCenter />} />
          <Route path="/share/:token" element={<SharePage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </Content>
    </Layout>
  );
}

export default function App() {
  return (
    <ConfigProvider
      locale={zhCN}
      theme={{ token: { colorPrimary: '#1677FF', borderRadius: 8, fontSize: 14 } }}
    >
      <HashRouter>
        <Shell />
      </HashRouter>
    </ConfigProvider>
  );
}
