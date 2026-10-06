import type { Stage } from '../lib/api';

const stageOrder = ['import_qc', 'component_precheck', 'solve', 'publish_checks'];
const labels: Record<string, string> = {
  import_qc: '分区质检',
  component_precheck: '连通分量预检',
  solve: '整体稀疏求解/QR诊断',
  publish_checks: '发布前核对'
};

export function StageTracker({ stages }: { stages: Stage[] }) {
  const byName = new Map(stages.map((stage) => [stage.name, stage]));
  return (
    <div className="stages">
      {stageOrder.map((name, index) => {
        const stage = byName.get(name);
        const status = stage?.status || 'pending';
        return (
          <div className={`stage stage-${status}`} key={name}>
            <div className="stage-index">{index + 1}</div>
            <div>
              <strong>{labels[name]}</strong>
              <small>
                {status} · attempt {stage?.attempt || 0}
              </small>
            </div>
          </div>
        );
      })}
    </div>
  );
}
