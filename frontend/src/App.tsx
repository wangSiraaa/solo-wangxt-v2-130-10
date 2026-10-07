import { useEffect, useMemo, useState } from 'react';
import type { ElementDefinition } from 'cytoscape';
import { api, type Job, type ResidualRow, type ResumeResponse, type StageAttempt } from './lib/api';
import { NetworkGraph } from './components/NetworkGraph';
import { StageTracker } from './components/StageTracker';
import { ResidualTable } from './components/ResidualTable';
import './styles.css';

const stageLabels: Record<string, string> = {
  import_qc: '分区质检',
  component_precheck: '连通分量预检',
  solve: '整体稀疏求解',
  publish_checks: '发布前核对'
};

const TERMINAL_STATUSES = ['completed', 'failed', 'awaiting_recovery', 'audited_only'];

export default function App() {
  const [projectId, setProjectId] = useState(1);
  const [elements, setElements] = useState<ElementDefinition[]>([]);
  const [job, setJob] = useState<Job | null>(null);
  const [residuals, setResiduals] = useState<ResidualRow[]>([]);
  const [attempts, setAttempts] = useState<StageAttempt[] | null>(null);
  const [message, setMessage] = useState('');

  useEffect(() => {
    api<{ nodes: unknown[]; edges: unknown[] }>(`/api/projects/${projectId}/topology`)
      .then((data) => setElements([...(data.nodes as ElementDefinition[]), ...(data.edges as ElementDefinition[])]))
      .catch((error) => setMessage(error.message));
  }, [projectId]);

  useEffect(() => {
    if (!job || TERMINAL_STATUSES.includes(job.status)) return;
    const timer = window.setInterval(async () => {
      const next = await api<Job>(`/api/jobs/${job!.id}`);
      setJob(next);
    }, 1500);
    return () => window.clearInterval(timer);
  }, [job]);

  const cyElements = useMemo(() => elements, [elements]);

  async function refreshJob(jobId: number) {
    setJob(await api<Job>(`/api/jobs/${jobId}`));
  }

  async function submitSnapshot() {
    setMessage('创建不可变快照并提交唯一任务代次...');
    const result = await api<{ job_id: number; deduplicated: boolean; snapshot_version: number }>(
      `/api/projects/${projectId}/jobs`,
      { method: 'POST' }
    );
    setMessage(result.deduplicated ? '重复提交已合并到既有代次' : `已启动快照 v${result.snapshot_version}`);
    await refreshJob(result.job_id);
  }

  async function resume() {
    if (!job) return;
    const result = await api<ResumeResponse>(`/api/jobs/${job.id}/resume`, { method: 'POST' });
    if (!result.enqueued) {
      setMessage('所有阶段均已确认，无需恢复（重复恢复不会创建同快照第二个任务）');
    } else {
      const skipped = result.skipped_confirmed.map((name) => stageLabels[name] || name).join('、') || '无';
      const retried = result.retry_stages.map((name) => stageLabels[name] || name).join('、');
      setMessage(`已恢复：跳过已确认阶段（${skipped}），仅重试 ${retried}`);
    }
    await refreshJob(job.id);
  }

  async function publish() {
    if (!job) return;
    try {
      const result = await api<{ publication_id: number; version: number }>(`/api/jobs/${job.id}/publish`, {
        method: 'POST',
        body: JSON.stringify({ confirm: true })
      });
      setMessage(`已发布成果版本 v${result.version}`);
      await refreshJob(job.id);
    } catch (error) {
      setMessage((error as Error).message);
    }
  }

  async function loadResiduals() {
    if (!job) return;
    setResiduals(await api<ResidualRow[]>(`/api/jobs/${job.id}/residuals?limit=100`));
  }

  async function toggleAttempts() {
    if (!job) return;
    if (attempts) {
      setAttempts(null);
      return;
    }
    setAttempts(await api<StageAttempt[]>(`/api/jobs/${job.id}/stage-attempts`));
  }

  const blocked = job !== null && !job.published && ['failed', 'awaiting_recovery'].includes(job.status);

  return (
    <main>
      <header>
        <h1>省级水准网成果平台</h1>
        <p>不可变观测/规则快照 · 稀疏加权最小二乘 · QR秩诊断 · 旧任务只审计不覆盖新草稿</p>
      </header>

      <section className="toolbar">
        <label>
          项目 ID
          <input value={projectId} onChange={(event) => setProjectId(Number(event.target.value))} type="number" />
        </label>
        <button onClick={submitSnapshot}>提交当前草稿快照</button>
        <button onClick={resume} disabled={!job || job.recovery_point === null}>
          从确认阶段恢复
        </button>
        <button onClick={loadResiduals} disabled={!job}>
          查看残差
        </button>
        <button onClick={toggleAttempts} disabled={!job}>
          {attempts ? '隐藏尝试记录' : '阶段尝试记录'}
        </button>
        <button onClick={publish} disabled={!job || job.status !== 'completed' || job.published} className="primary">
          发布成果
        </button>
      </section>

      {message && <div className="message">{message}</div>}

      {job &&
        (job.published ? (
          <div className="banner banner-published">已发布成果 · 快照 v{job.snapshot_version} · 代次 {job.generation_key}</div>
        ) : blocked ? (
          <div className="banner banner-blocked">
            <strong>未发布 · 发布阻断</strong>
            <span>
              {job.error_code ? `${job.error_code}：` : ''}
              {job.error_message || '阶段未全部确认'}
              {job.diagnostics?.blocked_components?.length
                ? ` · 阻塞分量 ${job.diagnostics.blocked_components.length} 个（无基准/病态，禁止正则化伪造高程）`
                : ''}
            </span>
            <span className="banner-note">失败任务不会被展示为已发布成果；阻断诊断持久保存，重启后仍可见。</span>
          </div>
        ) : (
          <div className="banner banner-draft">未发布 · {job.status === 'completed' ? '核对通过，可发布' : '流水线进行中'}</div>
        ))}

      <section className="grid">
        <div className="card">
          <h2>测点拓扑 / 问题子网</h2>
          <NetworkGraph elements={cyElements} />
        </div>
        <div className="card">
          <h2>任务阶段</h2>
          {job ? (
            <>
              <StageTracker stages={job.stages} recoveryPoint={job.recovery_point} jobStatus={job.status} onResume={resume} />
              <dl className="facts">
                <dt>状态</dt>
                <dd>{job.status}</dd>
                <dt>可恢复点</dt>
                <dd>{job.recovery_point ? stageLabels[job.recovery_point] || job.recovery_point : '无（全部已确认）'}</dd>
                <dt>快照版本</dt>
                <dd>v{job.snapshot_version}</dd>
                <dt>分量数</dt>
                <dd>{String(job.diagnostics?.component_count ?? '—')}</dd>
                <dt>阻塞分量</dt>
                <dd>{String(job.diagnostics?.blocked_components?.length ?? 0)}</dd>
                <dt>算法</dt>
                <dd>{String(job.algorithm.signature)}</dd>
                <dt>正则化</dt>
                <dd className="strong">禁止：{String(job.diagnostics?.regularization ?? 'none')}</dd>
              </dl>
            </>
          ) : (
            <p>提交快照后显示代次和阶段进度。</p>
          )}
        </div>
      </section>

      {attempts && (
        <section className="card">
          <h2>阶段尝试记录（审计 · 重试不覆盖旧尝试）</h2>
          <table className="residual-table attempts-table">
            <thead>
              <tr>
                <th>阶段</th>
                <th>尝试</th>
                <th>结果</th>
                <th>错误码</th>
                <th>诊断</th>
                <th>开始</th>
                <th>结束</th>
              </tr>
            </thead>
            <tbody>
              {attempts.map((attempt) => (
                <tr key={`${attempt.stage_name}-${attempt.attempt}`} className={attempt.status === 'failed' ? 'residual-bad' : ''}>
                  <td>{stageLabels[attempt.stage_name] || attempt.stage_name}</td>
                  <td>#{attempt.attempt}</td>
                  <td>{attempt.status}</td>
                  <td>{attempt.error_code || '—'}</td>
                  <td className="attempt-detail">{attempt.error_message || JSON.stringify(attempt.detail)}</td>
                  <td>{attempt.started_at ? new Date(attempt.started_at).toLocaleString() : '—'}</td>
                  <td>{attempt.completed_at ? new Date(attempt.completed_at).toLocaleString() : '—'}</td>
                </tr>
              ))}
              {attempts.length === 0 && (
                <tr>
                  <td colSpan={7}>尚无尝试记录</td>
                </tr>
              )}
            </tbody>
          </table>
        </section>
      )}

      {residuals.length > 0 && (
        <section className="card">
          <h2>改正数与残差追踪</h2>
          <ResidualTable rows={residuals} />
        </section>
      )}
    </main>
  );
}
