import { apiGet, apiSend } from './client';
import type { HealthInfo, SessionDetail, SessionItem } from './types';

export function fetchHealth() {
  return apiGet<HealthInfo>('/api/health');
}

export function createSession(title?: string) {
  return apiSend<SessionItem>('POST', '/api/session', title ? { title } : undefined);
}

export function listSessions(keyword?: string, includeArchived = false) {
  return apiGet<{ total: number; items: SessionItem[] }>('/api/session', {
    keyword,
    include_archived: includeArchived ? 'true' : 'false',
  });
}

export function getSession(sessionId: string) {
  return apiGet<SessionDetail>(`/api/session/${encodeURIComponent(sessionId)}`);
}

export function renameSession(sessionId: string, title: string) {
  return apiSend<SessionItem>('PATCH', `/api/session/${encodeURIComponent(sessionId)}`, {
    rename: { title },
  });
}

export function archiveSession(sessionId: string, archived: boolean) {
  return apiSend<SessionItem>('PATCH', `/api/session/${encodeURIComponent(sessionId)}`, {
    archive: { archived },
  });
}

export function deleteSession(sessionId: string) {
  return apiSend<{ id: string; deleted: boolean }>(
    'DELETE',
    `/api/session/${encodeURIComponent(sessionId)}`,
  );
}
