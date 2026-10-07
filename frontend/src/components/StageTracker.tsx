import type { Stage } from '../lib/api';

const stageOrder = ['import_qc', 'component_precheck', 'solve', 'publish_checks'];
const labels: Record<string, string> = {
  import_qc: '分区质检',
  component_precheck: '连通分量预检',
  solve: '整体稀疏求解/QR诊断',
  publish_checks: '发布前核对'
};
const statusLabels: Record<string, string> = {
  pending: '待执行',
  running: '运行中',
  confirmed: '已确认',
  failed: '失败',
  skipped: '跳过'
};

function formatTime(iso: string | null): string {
  if (!iso) return '—';
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString();
}

interface Props {
  stages: Stage[];
  recoveryPoint: string | null;
  jobStatus: string;
  onResume: () => void;
}

export function StageTracker({ stages, recoveryPoint, jobStatus, onResume }: Props) {
  const byName = new Map(stages.map((stage) => [stage.name, stage]));
  const resumable = recoveryPoint !== null && jobStatus !== 'running' && jobStatus !== 'completed';
  return (
    <div className="stages">
      {stageOrder.map((name, index) => {
        const stage = byName.get(name);
        const status = stage?.status || 'pending';
        const isRecoveryPoint = name === recoveryPoint;
        const failure =
          status === 'failed'
            ? stage?.last_attempt?.error_message ||
              (typeof stage?.detail?.error === 'string' ? stage.detail.error : null) ||
              stage?.last_attempt?.error_code
            : null;
        return (
          <div className={`stage stage-${status}${isRecoveryPoint ? ' stage-recovery' : ''}`} key={name}>
            <div className="stage-index">{index + 1}</div>
            <div className="stage-body">
              <strong>{labels[name]}</strong>
              <small>
                {statusLabels[status] || status} · 第 {stage?.attempt || 0} 次尝试
                {stage && stage.attempts_recorded > 0 && ` · 已记录 ${stage.attempts_recorded} 次尝试`}
              </small>
              {status === 'confirmed' && (
                <small className="stage-confirmed-note">确认于 {formatTime(stage?.confirmed_at ?? null)} · 已确认阶段绝不重跑</small>
              )}
              {failure && (
                <small className="stage-failure">
                  失败诊断：{stage?.last_attempt?.error_code ? `${stage.last_attempt.error_code} — ` : ''}
                  {failure}
                </small>
              )}
              {isRecoveryPoint && (
                <div className="recovery-point">
                  <span className="recovery-badge">可恢复点 · 恢复将从这里继续</span>
                  <button onClick={onResume} disabled={!resumable}>
                    从此处恢复
                  </button>
                </div>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}
