/**
 * 下钻接口封装（FR-L-2）。
 *
 * 接口只返回下一轮的问句，不执行查询——执行仍走 /api/chat 的既有链路，
 * 沿用同一套守卫、超时与落库。因此这里拿到的 question 要由调用方再次提交，
 * 而不是直接当作结果渲染。
 */

import { apiSend } from './client';
import type { DrilldownRequest, DrilldownResponse } from './types';

export function requestDrilldown(req: DrilldownRequest): Promise<DrilldownResponse> {
  return apiSend<DrilldownResponse>('POST', '/api/drilldown', req);
}
