// 数据源连接接口封装：内省测试 + 应用切换。
import type { SettingsView } from './settings';

export interface DatasourceTestResult {
  ok: boolean;
  dialect: string;
  table_count: number;
  tables: { name: string; fields: number; pk: string[] }[];
}

export interface DatasourceConnectResult {
  ok: boolean;
  dialect: string;
  table_count: number;
  metric_count: number;
  dimension_count: number;
}

export interface DatasourceRequest {
  dialect: string;
  path?: string;
  dsn?: string;
}

async function postJson<T>(url: string, body: unknown): Promise<T> {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    let detail = `请求失败（${res.status}）`;
    try {
      const data = await res.json();
      if (data?.detail) detail = data.detail;
    } catch {
      /* 保留默认文案 */
    }
    throw new Error(detail);
  }
  return res.json() as Promise<T>;
}

export function testDatasource(body: DatasourceRequest): Promise<DatasourceTestResult> {
  return postJson<DatasourceTestResult>('/api/datasource/test', body);
}

export function connectDatasource(body: DatasourceRequest): Promise<DatasourceConnectResult> {
  return postJson<DatasourceConnectResult>('/api/datasource/connect', body);
}

// 连接成功后刷新健康，让顶栏表数 / 模式标识更新
export type { SettingsView };
