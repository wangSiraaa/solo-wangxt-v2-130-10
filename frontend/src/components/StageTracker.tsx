import type { Job, Stage, StageAttempt } from '../lib/api';

const stageOrder = ['import_qc', 'component_precheck', 'solve', 'publish_checks'];
const labels: Record<string, string> = {
  import_qc: '分区质检',
  component_precheck: '连通分量预检',
  solve: '整体稀疏求解/QR诊断',
  publish_checks: '发布前核对'
};

const statusLabels: Record<string, string> = {
  pending: '待执行',
  confirmed: '已确认',
  running: '运行中',
  failed: '失败',
  skipped: '已跳过',
  interrupted: '已中断'
};

function formatTime(value: string | null): string {
  if (!value) return '—';
  return new Date(value).toLocaleString();
}

function compact(detail: Record<string, unknown>): string {
  return JSON.stringify(detail, null, 2);
}

function AttemptRecord({ attempt }: { attempt: StageAttempt }) {
  return (
    <li className={`attempt attempt-${attempt.status}`}>
      <strong>
        第 {attempt.attempt_number} 次 · {statusLabels[attempt.status] || attempt.status}
      </strong>
      <small>
        {formatTime(attempt.started_at)} → {formatTime(attempt.finished_at)}
        {attempt.error_code ? ` · ${attempt.error_code}` : ''}
      </small>
      {attempt.error_message && <em>{attempt.error_message}</em>}
      {Object.keys(attempt.detail || {}).length > 0 && <pre>{compact(attempt.detail)}</pre>}
    </li>
  );
}

export function StageTracker({
  job,
  onResume,
  resuming
}: {
  job: Job;
  onResume: () => void;
  resuming: boolean;
}) {
  const stages = job.stages;
  const byName = new Map(stages.map((stage) => [stage.name, stage]));
  const recoveryName = job.recovery_stage;
  const recoveryLabel = recoveryName ? labels[recoveryName] : null;

  return (
    <div className="stage-panel">
      <div className={`recovery-banner ${recoveryName ? 'is-recoverable' : ''}`}>
        {recoveryName ? (
          <>
            <div>
              <strong>可恢复点：{recoveryLabel}</strong>
              <small>
                已确认：
                {job.confirmed_stages.length > 0
                  ? job.confirmed_stages.map((name) => labels[name] || name).join('、')
                  : '无'}
                ；恢复只会重试该阶段及之后，已确认阶段不重跑。
              </small>
            </div>
            <button onClick={onResume} disabled={!job.can_resume || resuming}>
              {resuming ? '正在触发...' : `从${recoveryLabel}恢复`}
            </button>
          </>
        ) : (
          <>
            <strong>全部阶段已确认</strong>
            <small>重复恢复不会创建新 Job，也不会重跑确认产物。</small>
          </>
        )}
      </div>

      <div className={`publication-gate ${job.can_publish ? 'gate-ok' : 'gate-blocked'}`}>
        <strong>
          {job.has_publication
            ? '该 Job 已有发布记录'
            : job.can_publish
              ? '通过发布核对，尚未发布成果'
              : '发布阻断：这不是已发布成果'}
        </strong>
        {!job.can_publish && !job.has_publication && (
          <ul>
            {job.publication_blockers.length > 0 ? (
              job.publication_blockers.map((blocker) => <li key={blocker}>{blocker}</li>)
            ) : (
              <li>等待阶段完成后才能发布。</li>
            )}
          </ul>
        )}
        {job.error_code && !job.can_publish && <code>{job.error_code}</code>}
      </div>

      <div className="stages">
        {stageOrder.map((name, index) => {
          const stage: Stage | undefined = byName.get(name);
          const status = stage?.status || 'pending';
          const isRecoveryPoint = name === recoveryName;
          const oldAttempts = (stage?.attempts || []).slice(0, -1);
          return (
            <div
              className={`stage stage-${status} ${isRecoveryPoint ? 'stage-recovery' : ''}`}
              key={name}
            >
              <div className="stage-index">{index + 1}</div>
              <div className="stage-body">
                <div className="stage-heading">
                  <div>
                    <strong>{labels[name]}</strong>
                    <small>
                      {statusLabels[status] || status} · 当前第 {stage?.attempt || 0} 次尝试 · 重试{' '}
                      {stage?.retry_count || 0} 次
                      {isRecoveryPoint && ' · 恢复入口'}
                    </small>
                  </div>
                  {status === 'confirmed' && <span className="badge badge-ok">已确认，不重跑</span>}
                  {status === 'failed' && <span className="badge badge-bad">失败</span>}
                </div>

                <dl className="stage-times">
                  <dt>最近尝试</dt>
                  <dd>{formatTime(stage?.latest_attempt_at || null)}</dd>
                  <dt>确认时间</dt>
                  <dd>{formatTime(stage?.confirmed_at || null)}</dd>
                </dl>

                {stage?.failure_diagnostic && (
                  <div className="diagnostic">
                    <strong>失败诊断</strong>
                    {stage.failure_diagnostic.error_code && (
                      <code>{stage.failure_diagnostic.error_code}</code>
                    )}
                    {stage.failure_diagnostic.error_message && (
                      <p>{stage.failure_diagnostic.error_message}</p>
                    )}
                    {Object.keys(stage.failure_diagnostic.detail || {}).length > 0 && (
                      <pre>{compact(stage.failure_diagnostic.detail)}</pre>
                    )}
                  </div>
                )}

                {isRecoveryPoint && job.can_resume && (
                  <button className="stage-resume" onClick={onResume} disabled={resuming}>
                    {resuming ? '正在触发...' : '只重试此阶段之后'}
                  </button>
                )}

                {oldAttempts.length > 0 && (
                  <details className="attempt-history">
                    <summary>审计记录：保留 {oldAttempts.length} 次旧尝试</summary>
                    <ul>
                      {oldAttempts.map((attempt) => (
                        <AttemptRecord attempt={attempt} key={attempt.attempt_number} />
                      ))}
                    </ul>
                  </details>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
