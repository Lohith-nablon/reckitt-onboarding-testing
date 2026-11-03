import { get } from "../api";
import { Card, fmtTime, Kpi, useLoad } from "../components/ui";
import type { Nav } from "../App";

export function Home({ nav, sessions }: { nav: Nav; sessions: any[] }) {
  const lib = useLoad(() => get<any[]>("/library/mappings"), []);
  const cw = useLoad(() => get<Record<string, { accepted: number; rejected: number }>>("/library/crosswalk"), []);
  const learned = Object.values(cw.data ?? {}).reduce((a, x) => a + x.accepted, 0);
  return (
    <>
      <div className="banner info">
        <div><b>From retailer files to bronze-ready output.</b> AI suggests, people decide at three gates, and every decision is traced.
          The first file of a layout is reviewed; repeats reuse the approved mapping. Target: onboarding from 10 days to 2–3 days.</div>
      </div>
      <div className="kpis">
        <Kpi v={sessions.length} l="Onboarding sessions" />
        <Kpi v={sessions.filter((s) => s.status === "signed_off").length} l="Signed off" />
        <Kpi v={lib.data?.length ?? "–"} l="Layouts in the mapping library" accent />
        <Kpi v={learned} l="Codes learned (crosswalk)" accent />
      </div>
      <Card title="Sessions" actions={<button className="primary" onClick={() => nav("/new")}>New onboarding</button>} flush>
        {sessions.length === 0 ? <div className="empty">No sessions yet. Start one to onboard a retailer file.</div> :
          <table className="t"><thead><tr><th>Name</th><th>Retailer</th><th>Market</th><th>Files</th><th>Stage</th><th>Updated</th></tr></thead>
            <tbody>{sessions.map((s) => (
              <tr key={s.session_id} className="click" onClick={() => nav(`/s/${s.session_id}/${s.stage === "done" ? "signoff" : s.stage}`)}>
                <td className="strong">{s.name}</td><td>{s.retailer}</td><td>{s.market}</td><td>{s.files}</td>
                <td>{s.status === "signed_off" ? <span className="tag blue">Signed off</span> : <span className="tag">{s.stage}</span>}</td>
                <td className="muted">{fmtTime(s.updated_at)}</td></tr>))}</tbody></table>}
      </Card>
      <Card title="Mapping library (M8)" flush>
        {(lib.data ?? []).length === 0 ? <div className="empty">Empty. Every signed-off mapping lands here and is reused for the next file with the same layout.</div> :
          <table className="t"><thead><tr><th>Retailer</th><th>Target</th><th>Layout</th><th>Version</th><th>Approved by</th><th>Reused</th></tr></thead>
            <tbody>{lib.data!.map((e) => (
              <tr key={e.key}><td>{e.retailer} · {e.market}</td><td>{e.target_table}</td><td className="mono">{e.fingerprint}</td>
                <td>v{e.version}</td><td>{e.approved_by}<div className="small muted">{fmtTime(e.approved_at)}</div></td><td>{e.times_reused}×</td></tr>))}</tbody></table>}
      </Card>
    </>
  );
}
