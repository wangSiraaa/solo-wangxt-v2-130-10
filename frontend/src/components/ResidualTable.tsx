import type { ResidualRow } from '../lib/api';

export function ResidualTable({ rows }: { rows: ResidualRow[] }) {
  return (
    <table className="residual-table">
      <thead>
        <tr>
          <th>测段</th>
          <th>原始高差 (m)</th>
          <th>平差后理论高差 (m)</th>
          <th>改正数 v (m)</th>
          <th>残差</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr key={row.line_code}>
            <td>{row.line_code}</td>
            <td>{row.observed_delta_m.toFixed(6)}</td>
            <td>{row.adjusted_delta_m?.toFixed(6) ?? '—'}</td>
            <td>{row.correction_m?.toFixed(6) ?? '—'}</td>
            <td className={Math.abs(row.residual ?? 0) > 0.01 ? 'residual-bad' : ''}>
              {row.residual?.toFixed(6) ?? '—'}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
