const API_BASE = '';

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`${response.status}: ${text}`);
  }
  return response.json();
}

export interface StageAttempt {
  attempt_number: number;
  status: string;
  detail: Record<string, unknown>;
  error_code: string | null;
  error_message: string | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string | null;
}

export interface Stage {
  name: string;
  status: string;
  attempt: number;
  retry_count: number;
  detail: Record<string, unknown>;
  started_at: string | null;
  confirmed_at: string | null;
  completed_at: string | null;
  latest_attempt: StageAttempt;
  latest_attempt_at: string | null;
  failure_diagnostic: {
    detail: Record<string, unknown>;
    error_code: string | null;
    error_message: string | null;
  } | null;
  attempts: StageAttempt[];
}

export interface Job {
  id: number;
  status: string;
  current_stage: string;
  generation_key: string;
  snapshot_version: number;
  input_summary: Record<string, number | string>;
  algorithm: Record<string, unknown>;
  diagnostics: Record<string, any>;
  error_code: string | null;
  error_message: string | null;
  recovery_stage: string | null;
  confirmed_stages: string[];
  can_resume: boolean;
  can_publish: boolean;
  publication_blockers: string[];
  has_publication: boolean;
  stages: Stage[];
}

export interface ResidualRow {
  line_code: string;
  observed_delta_m: number;
  adjusted_delta_m: number | null;
  correction_m: number | null;
  residual: number | null;
}
