import { apiGet, apiSend } from './client';

export interface SettingsView {
  has_api_key: boolean;
  llm_base_url: string;
  llm_model: string;
  llm_mock: boolean;
  llm_fallback_model: string;
}

export interface SettingsPatch {
  llm_api_key?: string;
  llm_base_url?: string;
  llm_model?: string;
  llm_mock?: boolean;
  llm_fallback_model?: string;
}

export function getSettings() {
  return apiGet<SettingsView>('/api/settings');
}

export function updateSettings(patch: SettingsPatch) {
  return apiSend<SettingsView>('POST', '/api/settings', patch);
}
