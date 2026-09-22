/**
 * 语义层接口封装。
 *
 * 用于口径面板（FR-D-3）展示指标表达式、单位、分母、必备维度与枚举字段的
 * 划分依据。这些字段不在 chat 接口的返回体里（避免每轮重复拉），改成面板
 * 展开时按需请求，并用模块级 Promise 缓存减少重复网络。
 *
 * 字段表（enums）和口径面板（metrics）分开拉，是因为大表场景两个 endpoint
 * 数据量都不小，分开缓存可以让面板只刷新某个指标时不必连带刷整张表。
 */

import { apiGet } from './client';

export interface MetricDoc {
  name: string;
  display: string;
  expression: string;
  unit?: string;
  caveats?: string;
  denominator?: string;
  required_filters?: string[];
  dimensions?: string[];
  examples?: { q: string; sql: string }[];
}

export interface FieldDoc {
  name: string;
  type: string;
  display?: string;
  definition?: string;
  enum?: Record<string, string>;
  ref?: string;
  note?: string;
}

export interface TableDoc {
  name: string;
  display: string;
  grain?: string;
  join?: string;
  fields: FieldDoc[];
}

let metricsPromise: Promise<MetricDoc[]> | null = null;
let tablesPromise: Promise<TableDoc[]> | null = null;

export function fetchMetrics(): Promise<MetricDoc[]> {
  if (!metricsPromise) {
    metricsPromise = apiGet<{ items: MetricDoc[] }>('/api/semantic/metrics').then(
      (res) => res.items,
    );
  }
  return metricsPromise;
}

export function fetchTables(): Promise<TableDoc[]> {
  if (!tablesPromise) {
    tablesPromise = apiGet<{ items: TableDoc[] }>('/api/semantic/tables').then((res) => res.items);
  }
  return tablesPromise;
}

/** 取一个指标的完整定义；未登记返回 null。 */
export async function getMetric(name: string): Promise<MetricDoc | null> {
  const all = await fetchMetrics();
  return all.find((m) => m.name === name) ?? null;
}

/** 取与某指标关联的若干张表的字段集。用于在面板里展示来源表结构。 */
export async function getTablesForMetric(names: string[]): Promise<TableDoc[]> {
  if (names.length === 0) return [];
  const tables = await fetchTables();
  const set = new Set(names);
  return tables.filter((t) => set.has(t.name));
}

/** 收集某指标里所有出现过的 enum 字段的 definition，用于面板「枚举划分依据」项。 */
export async function getEnumDefinitions(tables: TableDoc[]): Promise<FieldDoc[]> {
  const out: FieldDoc[] = [];
  for (const t of tables) {
    for (const f of t.fields) {
      if (f.enum && f.definition) out.push(f);
    }
  }
  return out;
}
