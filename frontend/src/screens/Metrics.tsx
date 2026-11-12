import { get } from "../api";
import { Card, ErrorBanner, fmtTime, Kpi, Loading, pct, Rag, useLoad } from "../components/ui";
import type { Nav } from "../App";

/** System-level KPIs: how much the AI maps on its own, how close it is to Veritas, how many
 *  manual inputs onboarding takes, per month and over time. */
export function MetricsScreen({ nav }: { nav: Nav }) {
  const m = useLoad(() => get<any>("/metrics"), []);
  if (m.error) return <ErrorBanner error={m.error} />;
  if (!m.data) return <Loading />;
  const t = m.data.totals;
  return (
    <>
      <div className="kpis">
        <Kpi v={pct(t.auto_ready)} l="AI auto-ready mapping (avg)" rag={t.rag} />
        <Kpi v={pct(t.veritas_match)} l="equal to Veritas reference (avg)" />
        <Kpi v={t.files} l={`files onboarded · ${t.known_layout_files} known layout`} />
        <Kpi v={t.signed_off} l={`signed off of ${t.sessions} sessions`} />
        <Kpi v={t.rows_loaded.toLocaleString()} l="rows loaded to bronze" />
        <Kpi v={t.manual_inputs} l={`manual inputs${t.manual_per_file != null ? ` · ${t.manual_per_file} per file` : ""}`} accent />
        <Kpi v={`${t.effort_minutes} min`} l="review effort still open" />
        <Kpi v={t.hours_to_signoff == null ? "–" : `${t.hours_to_signoff} h`} l="start to sign-off (avg)" />
      </div>
      <Card title="Accuracy over time">
        {m.data.trend.length < 2 ? <div className="muted">Shown once there are two or more sessions.</div> : <TrendChart trend={m.data.trend} green={m.data.thresholds.green} amber={m.data.thresholds.amber} />}
      </Card>
      <Card title="Monthly roll-up" flush>
        <table className="t"><thead><tr><th>Month</th><th>Sessions</th><th>Files</th><th>Signed off</th><th>Rows loaded</th><th>Manual inputs</th><th>AI auto-ready</th><th>Veritas match</th></tr></thead>
          <tbody>{m.data.monthly.map((r: any) => (
            <tr key={r.month}><td className="strong">{r.month}</td><td>{r.sessions}</td><td>{r.files}</td><td>{r.signed_off}</td>
              <td>{r.rows_loaded.toLocaleString()}</td><td>{r.manual_inputs}</td>
              <td><Rag r={r.auto_ready == null ? null : r.auto_ready >= m.data.thresholds.green ? "green" : r.auto_ready >= m.data.thresholds.amber ? "amber" : "red"} label={pct(r.auto_ready)} /></td>
              <td>{pct(r.veritas_match)}</td></tr>))}</tbody></table>
      </Card>
      <Card title="Sessions" flush>
        {m.data.sessions.length === 0 ? <div className="empty">No sessions yet.</div> :
          <table className="t"><thead><tr><th>Session</th><th>Stage</th><th>Auto-ready</th><th>To check</th><th>Manual inputs</th><th>Rows</th><th>Hours to Gate A</th><th>Started</th></tr></thead>
            <tbody>{m.data.sessions.map((s: any) => (
              <tr key={s.session_id} className="click" onClick={() => nav(`/s/${s.session_id}/${s.stage === "done" ? "signoff" : s.stage === "start" ? "start" : s.stage}`)}>
                <td><b>{s.name}</b><div className="small muted">{s.retailer} · {s.market}</div></td>
                <td>{s.status === "signed_off" ? <span className="tag blue">Signed off</span> : <span className="tag">{s.stage}</span>}</td>
                <td><Rag r={s.mapping.rag} label={pct(s.mapping.auto_ready)} /></td><td>{s.mapping.to_review}</td>
                <td>{s.manual}</td><td>{s.rows_loaded.toLocaleString()}</td><td>{s.timings.to_gate_a_h ?? "–"}</td>
                <td className="muted">{fmtTime(s.created_at)}</td></tr>))}</tbody></table>}
      </Card>
    </>
  );
}

/** Auto-ready share and Veritas match per session, oldest to newest, with the green and amber lines. */
function TrendChart({ trend, green, amber }: { trend: any[]; green: number; amber: number }) {
  const W = 760, H = 220, L = 40, R = 12, T = 12, B = 34;
  const x = (i: number) => L + (trend.length === 1 ? 0 : (i / (trend.length - 1)) * (W - L - R));
  const y = (v: number) => T + (1 - v) * (H - T - B);
  const line = (key: string) => trend.map((p, i) => p[key] == null ? null : `${x(i)},${y(p[key])}`).filter(Boolean).join(" ");
  return (
    <div className="chart">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="AI auto-ready share and Veritas match per session">
        {[0, 0.5, 1].map((v) => <g key={v}><line className="axis" x1={L} x2={W - R} y1={y(v)} y2={y(v)} /><text x={L - 6} y={y(v) + 4} textAnchor="end">{v * 100}%</text></g>)}
        <line x1={L} x2={W - R} y1={y(green)} y2={y(green)} stroke="var(--rag-green)" strokeDasharray="4 4" />
        <line x1={L} x2={W - R} y1={y(amber)} y2={y(amber)} stroke="var(--rag-amber)" strokeDasharray="4 4" />
        <polyline points={line("veritas_match")} fill="none" stroke="var(--gray)" strokeWidth={1.5} />
        <polyline points={line("auto_ready")} fill="none" stroke="var(--blue)" strokeWidth={2} />
        {trend.map((p, i) => p.auto_ready == null ? null :
          <circle key={p.session_id} cx={x(i)} cy={y(p.auto_ready)} r={p.known_layout ? 5 : 3.5} fill={p.known_layout ? "var(--white)" : "var(--blue)"} stroke="var(--blue)" strokeWidth={1.5}>
            <title>{`${p.name}: ${Math.round(p.auto_ready * 100)}% auto-ready, ${p.manual_inputs} manual inputs${p.known_layout ? " (known layout)" : ""}`}</title></circle>)}
        {trend.map((p, i) => (i === 0 || i === trend.length - 1) ? <text key={`l${i}`} x={x(i)} y={H - 10} textAnchor={i === 0 ? "start" : "end"}>{p.at.slice(0, 10)}</text> : null)}
      </svg>
      <div className="legend"><span style={{ ["--c" as any]: "var(--blue)" }}>AI auto-ready (open circle = known layout)</span>
        <span style={{ ["--c" as any]: "var(--gray)" }}>Veritas match</span>
        <span style={{ ["--c" as any]: "var(--rag-green)" }}>high-confidence line {Math.round(green * 100)}%</span>
        <span style={{ ["--c" as any]: "var(--rag-amber)" }}>medium line {Math.round(amber * 100)}%</span></div>
    </div>
  );
}
