import { get, post } from "../api";
import { Card, ErrorBanner, Kpi, Loading, useAction, useLoad } from "../components/ui";
import type { ScreenProps } from "../App";

/** Hand-off between Phase 1 and Phase 2: Reckitt runs its pipeline on the published SQL; the
 *  app checks Unity Catalog for the loaded rows, then a person confirms and Phase 2 starts. */
export function PipelineScreen({ ov, refresh, nav }: ScreenProps) {
  const s = ov.session;
  const sid = s.session_id;
  const st = useLoad(() => s.gates.A.status === "approved" ? get<any>(`/sessions/${sid}/pipeline`) : Promise.resolve(null), [sid, s.version]);
  const a = useAction();
  if (s.gates.A.status !== "approved") return <div className="banner">Approve the mapping (Gate A) first. That writes the mapping JSON and the SQL the pipeline runs.</div>;
  if (st.error) return <ErrorBanner error={st.error} />;
  if (!st.data) return <Loading />;
  const d = st.data;
  const after = ["harmonisation", "validate", "signoff", "done"].includes(s.stage);
  const loaded = d.items.filter((i: any) => i.loaded).length;
  return (
    <>
      <div className="banner info"><div><b>Reckitt runs its Databricks pipeline on these SQL files.</b> In the POC this is a manual run; the app never triggers it.
        Each script deletes its own submission first, so running it twice is safe. When the rows are in Unity Catalog, check and continue.</div></div>
      <div className="kpis">
        <Kpi v={`${loaded}/${d.items.length}`} l="bronze tables loaded" accent />
        <Kpi v={d.items.reduce((n: number, i: any) => n + (i.loaded_rows ?? 0), 0).toLocaleString()} l="rows found in bronze" />
        <Kpi v={d.warehouse?.kind === "local" ? "Local stand-in" : "SQL warehouse"} l={d.warehouse?.kind === "local" ? "DuckDB, for development" : d.warehouse?.host ?? ""} />
      </div>
      <Card title="SQL for the pipeline" flush actions={<>
        <button disabled={a.busy} onClick={() => st.reload()}>Check Unity Catalog again</button>
        {d.can_run_locally && <button disabled={a.busy} onClick={() => a.run(async () => { await post(`/sessions/${sid}/pipeline/run-locally`); st.reload(); refresh(); }, "SQL run on the local stand-in")}>
          {a.busy ? <><span className="spin" /> Running…</> : d.warehouse?.kind === "local" ? "Run pipeline locally (dev stand-in)" : "Run the SQL on the warehouse (simulation)"}</button>}
      </>}>
        <table className="t"><thead><tr><th>File → bronze table</th><th>Submission</th><th>SQL</th><th>Rows in bronze</th></tr></thead>
          <tbody>{d.items.map((i: any) => (
            <tr key={i.mapping_id}>
              <td><b>{i.file}</b><div className="mono small">{i.target_fqn}</div></td>
              <td className="mono small">{i.submission_id}</td>
              <td className="small"><a href={`/api/sessions/${sid}/pipeline/sql/${i.mapping_id}`}>Download</a>
                <div className="mono muted" style={{ wordBreak: "break-all" }}>{i.sql_uri}</div>
                {i.warnings.length > 0 && <div><span className="tag flag">check</span> {i.warnings.join("; ")}</div>}</td>
              <td>{i.loaded ? <b>{i.loaded_rows.toLocaleString()}</b> : <span className="tag dashed">not loaded yet</span>}
                {i.error && <div className="small muted">{i.error}</div>}</td>
            </tr>))}</tbody></table>
      </Card>
      <ErrorBanner error={a.error} />
      <div className="gate">
        <div><div className="strong">Pipeline load</div>
          <div className="small muted">{after ? `Confirmed by ${s.outputs?.pipeline?.confirmed_by ?? ""}. Phase 2 is open.` : d.all_loaded ? "All tables loaded. Confirm to start harmonisation." : "Waiting for the pipeline run."}</div></div>
        {after ? <button className="primary" onClick={() => nav(`/s/${sid}/harmonisation`)}>Continue to Harmonisation</button>
          : <button className="primary" disabled={!d.all_loaded || a.busy} onClick={() => a.run(async () => { await post(`/sessions/${sid}/pipeline/confirm`, {}); refresh(); nav(`/s/${sid}/harmonisation`); }, "Load confirmed: Phase 2 open")}>
            Confirm load and start Phase 2</button>}
      </div>
    </>
  );
}
