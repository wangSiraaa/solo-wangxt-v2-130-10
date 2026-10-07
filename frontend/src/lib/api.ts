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
  stage_name: string;
  attempt: number;
  status: string;
  detail: Record<string, any>;
  error_code: string | null;
  error_message: string | null;
  started_at: string | null;
  completed_at: string | null;
}

export interface Stage {
  name: string;
  status: string;
  attempt: number;
  detail: Record<string, any>;
  started_at: string | null;
  confirmed_at: string | null;
  completed_at: string | null;
  attempts_recorded: number;
  last_attempt: StageAttempt | null;
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
  attempt: number;
  started_at: string | null;
  finished_at: string | null;
  recovery_point: string | null;
  published: boolean;
  stages: Stage[];
}

export interface ResumeResponse {
  job_id: number;
  recovery_point: string | null;
  skipped_confirmed: string[];
  retry_stages: string[];
  enqueued: boolean;
  deduplicated: boolean;
}

export interface ResidualRow {
  line_code: string;
  observed_delta_m: number;
  adjusted_delta_m: number | null;
  correction_m: number | null;
  residual: number | null;
}
