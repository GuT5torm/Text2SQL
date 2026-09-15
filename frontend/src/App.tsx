import { useEffect, useState } from 'react';
import { HashRouter, Navigate, Route, Routes, useLocation } from 'react-router-dom';
import { Button, ConfigProvider, Layout, Space, Tag, Tooltip, Typography } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import { ChatPage } from '@/pages/ChatPage';
import { HistoryPage } from '@/pages/HistoryPage';
import { fetchHealth } from '@/api/session';
import type { HealthInfo } from '@/api/types';

const { Header, Content } = Layout;
const { Text } = Typography;

function Nav() {
  const { pathname, search } = useLocation();
  const onChat = pathname === '/' || search.includes('session=');
  return (
    <Space size={4}>
      <Button type={onChat ? 'primary' : 'text'} href="#/">
        对话
      </Button>
      <Button type={pathname === '/history' ? 'primary' : 'text'} href="#/history">
        历史会话
      </Button>
    </Space>
  );
}

function Shell() {
  const [health, setHealth] = useState<HealthInfo | null>(null);

  useEffect(() => {
    fetchHealth()
      .then(setHealth)
      .catch(() => setHealth(null));
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
        </Space>
        <Nav />
      </Header>
      <Content style={{ padding: 16, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
        <Routes>
          <Route path="/" element={<ChatPage />} />
          <Route path="/history" element={<HistoryPage />} />
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
