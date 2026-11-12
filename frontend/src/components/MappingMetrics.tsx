import { get } from "../api";
import { Card, Kpi, pct, Rag, useLoad } from "./ui";

/** Mapping readiness for one session: how much the AI mapped with high confidence, what a person
 *  still has to check (and roughly how long), and how many manual inputs it took so far. */
export function MappingMetrics({ sid, version, compact }: { sid: string; version: number; compact?: boolean }) {
  const m = useLoad(() => get<any>(`/sessions/${sid}/metrics`), [sid, version]);
  if (!m.data) return null;
  const x = m.data.mapping;
  const th = m.data.thresholds;
  const need = x.total - x.unmapped_optional;
  const seg = (n: number) => need ? `${(n / need) * 100}%` : "0";
  const man = m.data.manual;
  return (
    <Card title={<>Mapping readiness <Rag r={x.rag} /></>}
      actions={<span className="small muted">green ≥ {Math.round(th.green * 100)}% · amber ≥ {Math.round(th.amber * 100)}% · red below</span>}>
      {need === 0 ? <div className="muted">No mapping proposed yet. Run AI Discovery and mapping.</div> : <div className="stack">
        <div className="kpis">
          <Kpi v={pct(x.auto_ready)} l="mapped by AI at ≥ 90% (auto-ready)" rag={x.rag} />
          <Kpi v={pct(x.pct_ge_80)} l="mapped at ≥ 80%" />
          <Kpi v={pct(x.pct_ge_70)} l="mapped at ≥ 70%" />
          <Kpi v={x.to_review} l="columns to check" accent={x.to_review > 0} />
          <Kpi v={`${x.effort_minutes} min`} l={`estimated effort (${th.minutes_per_review} min per column)`} />
          <Kpi v={man.total} l={`manual inputs${man.per_file != null ? ` · ${man.per_file} per file` : ""}`} />
          {x.veritas_match != null && <Kpi v={pct(x.veritas_match)} l="equal to Veritas reference" />}
        </div>
        <div>
          <div className="stackbar" title="target columns that need a mapping">
            <i className="green" style={{ width: seg(x.green) }} /><i className="amber" style={{ width: seg(x.amber) }} />
            <i className="red" style={{ width: seg(x.red) }} /><i className="person" style={{ width: seg(x.person) }} />
            <i className="none" style={{ width: seg(x.unmapped_mandatory) }} />
          </div>
          <div className="legend" style={{ marginTop: 6 }}>
            <span style={{ ["--c" as any]: "var(--rag-green)" }}>{x.green} green</span>
            <span style={{ ["--c" as any]: "var(--rag-amber)" }}>{x.amber} amber</span>
            <span style={{ ["--c" as any]: "var(--rag-red)" }}>{x.red} red</span>
            <span style={{ ["--c" as any]: "var(--blue)" }}>{x.person} set by a person</span>
            <span style={{ ["--c" as any]: "var(--salt)" }}>{x.unmapped_mandatory} mandatory unmapped</span>
            <span>{x.unmapped_optional} optional left empty (not counted)</span>
          </div>
        </div>
        {!compact && m.data.mappings.length > 1 && (
          <table className="t"><thead><tr><th>File → target</th><th>Auto-ready</th><th>Green / amber / red</th><th>To check</th><th>Veritas</th></tr></thead>
            <tbody>{m.data.mappings.map((r: any) => (
              <tr key={r.mapping_id}><td><b>{r.file_name}</b>{r.sheet && ` (${r.sheet})`} → {r.target_table}</td>
                <td><Rag r={r.rag} label={pct(r.auto_ready)} /></td><td>{r.green} / {r.amber} / {r.red}</td>
                <td>{r.to_review}</td><td>{pct(r.veritas_match)}</td></tr>))}</tbody></table>)}
        {compact && m.data.discovery.columns > 0 && <div className="small muted">Source columns (this screen): {m.data.discovery.columns} found,
          {" "}{m.data.discovery.needs_review ?? 0} flagged to check; flagged columns not used by a mandatory target don't add review effort above.</div>}
        {Object.keys(man.by_step).length > 0 && <div className="small muted">Manual inputs by step: {Object.entries(man.by_step).map(([k, v]) => `${k} ${v}`).join(" · ")}</div>}
      </div>}
    </Card>
  );
}
