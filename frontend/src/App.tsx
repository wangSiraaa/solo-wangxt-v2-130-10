import { useEffect, useMemo, useState } from 'react';
import type { ElementDefinition } from 'cytoscape';
import { api, type Job, type ResidualRow } from './lib/api';
import { NetworkGraph } from './components/NetworkGraph';
import { StageTracker } from './components/StageTracker';
import { ResidualTable } from './components/ResidualTable';
import './styles.css';

export default function App() {
  const [projectId, setProjectId] = useState(1);
  const [elements, setElements] = useState<ElementDefinition[]>([]);
  const [job, setJob] = useState<Job | null>(null);
  const [residuals, setResiduals] = useState<ResidualRow[]>([]);
  const [message, setMessage] = useState('');

  useEffect(() => {
    api<{ nodes: unknown[]; edges: unknown[] }>(`/api/projects/${projectId}/topology`)
      .then((data) => setElements([...(data.nodes as ElementDefinition[]), ...(data.edges as ElementDefinition[])]))
      .catch((error) => setMessage(error.message));
  }, [projectId]);

  useEffect(() => {
    if (!job || ['completed', 'failed'].includes(job.status)) return;
    const timer = window.setInterval(async () => {
      const next = await api<Job>(`/api/jobs/${job!.id}`);
      setJob(next);
    }, 1500);
    return () => window.clearInterval(timer);
  }, [job]);

  const cyElements = useMemo(() => elements, [elements]);

  async function submitSnapshot() {
    setMessage('创建不可变快照并提交唯一任务代次...');
    const result = await api<{ job_id: number; deduplicated: boolean; snapshot_version: number }>(
      `/api/projects/${projectId}/jobs`,
      { method: 'POST' }
    );
    setMessage(result.deduplicated ? '重复提交已合并到既有代次' : `已启动快照 v${result.snapshot_version}`);
    const detail = await api<Job>(`/api/jobs/${result.job_id}`);
    setJob(detail);
  }

  async function resume() {
    if (!job) return;
    await api(`/api/jobs/${job.id}/resume`, { method: 'POST' });
    setMessage('已从最后一个已确认阶段恢复');
  }

  async function publish() {
    if (!job) return;
    try {
      const result = await api<{ publication_id: number; version: number }>(`/api/jobs/${job.id}/publish`, {
        method: 'POST',
        body: JSON.stringify({ confirm: true })
      });
      setMessage(`已发布成果版本 v${result.version}`);
    } catch (error) {
      setMessage((error as Error).message);
    }
  }

  async function loadResiduals() {
    if (!job) return;
    setResiduals(await api<ResidualRow[]>(`/api/jobs/${job.id}/residuals?limit=100`));
  }

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
        <button onClick={resume} disabled={!job}>
          从确认阶段恢复
        </button>
        <button onClick={loadResiduals} disabled={!job}>
          查看残差
        </button>
        <button onClick={publish} disabled={job?.status !== 'completed'} className="primary">
          发布成果
        </button>
      </section>

      {message && <div className="message">{message}</div>}

      <section className="grid">
        <div className="card">
          <h2>测点拓扑 / 问题子网</h2>
          <NetworkGraph elements={cyElements} />
        </div>
        <div className="card">
          <h2>任务阶段</h2>
          {job ? (
            <>
              <StageTracker stages={job.stages} />
              <dl className="facts">
                <dt>状态</dt>
                <dd>{job.status}</dd>
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

      {residuals.length > 0 && (
        <section className="card">
          <h2>改正数与残差追踪</h2>
          <ResidualTable rows={residuals} />
        </section>
      )}
    </main>
  );
}
