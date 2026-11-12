import { useEffect } from "react";
import { get, post } from "../api";
import { Card, ErrorBanner, fmtTime, Kpi, Loading, useAction, useLoad } from "../components/ui";
import type { ScreenProps } from "../App";

const RUN_LABEL: Record<string, string> = { pending: "Queued", running: "Running", success: "Succeeded", failed: "Failed" };

/** Hand-off between Phase 1 and Phase 2. pipeline.mode decides who loads bronze:
 *  job          the app started the pipeline job; this screen watches the run and Phase 2 starts by itself;
 *  watch_table  Reckitt runs its pipeline; the app checks bronze and continues when the rows are there;
 *  manual       Reckitt runs its pipeline; a person checks and confirms. */
export function PipelineScreen({ ov, refresh, nav }: ScreenProps) {
  const s = ov.session;
  const sid = s.session_id;
  const st = useLoad(() => s.gates.A.status === "approved" ? get<any>(`/sessions/${sid}/pipeline`) : Promise.resolve(null), [sid, s.version]);
  const a = useAction();
  const d = st.data;
  const waiting = s.stage === "pipeline" && d && d.mode !== "manual" && (d.mode === "watch_table" || ["pending", "running"].includes(d.run?.state));
  // poll while the run is going (job) or until rows show up (watch_table)
  useEffect(() => {
    if (!waiting) return;
    const t = setInterval(async () => {
      try {
        const nd = await get<any>(`/sessions/${sid}/pipeline`);
        st.setData(nd);
        if (nd.stage !== "pipeline" || nd.run?.state !== d?.run?.state) refresh();
      } catch { /* keep polling */ }
    }, Math.max(3, d?.poll_seconds ?? 10) * 1000);
    return () => clearInterval(t);
  }, [waiting, sid, d?.run?.state, d?.poll_seconds]);
  // the status call can move the session on (load detected, Phase 2 started): pick that up
  useEffect(() => { if (d && d.stage !== s.stage) refresh(); }, [d?.stage, s.stage]);
  if (s.gates.A.status !== "approved") return <div className="banner">Approve the mapping (Gate A) first. That writes the mapping JSON and the SQL the pipeline runs.</div>;
  if (st.error) return <ErrorBanner error={st.error} />;
  if (!d) return <Loading />;
  const after = ["harmonisation", "validate", "signoff", "done"].includes(s.stage);
  const loaded = d.items.filter((i: any) => i.loaded).length;
  const run = d.run;
  const auto = (s.outputs as any)?.phase2_auto;
  return (
    <>
      {d.mode === "job" ? (
        <div className="banner info"><div><b>The pipeline job runs automatically.</b> Gate A started the job on the generated SQL. This screen checks the run every {d.poll_seconds} s;
          when it succeeds and the rows are in bronze, Phase 2 starts{d.auto_phase2 ? " by itself (code matching and value standards, up to Gate B)" : ""}.</div></div>
      ) : d.mode === "watch_table" ? (
        <div className="banner info"><div><b>Reckitt runs its pipeline on these SQL files.</b> The app triggers nothing; it checks Unity Catalog every {d.poll_seconds} s and continues
          {d.auto_phase2 ? " into Phase 2" : ""} once this submission's rows are in bronze.</div></div>
      ) : (
        <div className="banner info"><div><b>Reckitt runs its Databricks pipeline on these SQL files.</b> In the POC this is a manual run; the app never triggers it.
          Each script deletes its own submission first, so running it twice is safe. When the rows are in Unity Catalog, check and continue.</div></div>
      )}
      {d.mode === "job" && (
        <Card title="Pipeline job run" actions={<>
          {run?.url && <a className="btn" style={{ textDecoration: "none" }} href={run.url} target="_blank" rel="noreferrer">Open run in Databricks</a>}
          {s.stage === "pipeline" && run?.state === "failed" && <button className="primary" disabled={a.busy}
            onClick={() => a.run(async () => { await post(`/sessions/${sid}/pipeline/start-job`); st.reload(); refresh(); }, "Pipeline job started again")}>
            {a.busy ? <><span className="spin" /> Starting…</> : "Run the job again"}</button>}
        </>}>
          {!run ? <div className="muted">No run yet.</div> : <div className="stack">
            <div className="row">
              <span className="runstate">{["pending", "running"].includes(run.state) && <span className="spin" />}
                {run.state === "success" ? <span className="tag blue">{RUN_LABEL[run.state]}</span> : run.state === "failed" ? <span className="tag flag">{RUN_LABEL[run.state]}</span> : <span className="tag">{RUN_LABEL[run.state] ?? run.state}</span>}</span>
              <span className="small muted">Run {run.run_id ?? "–"} · attempt {run.attempt} · started {fmtTime(run.started_at)} by {run.started_by}{run.finished_at && ` · finished ${fmtTime(run.finished_at)}`}{run.runner === "local" && " · local stand-in"}</span>
            </div>
            {run.message && <div className="small"><b>{run.state === "failed" ? "Error" : "Note"}:</b> {run.message}</div>}
            {run.poll_error && <div className="small muted">Could not read the run state: {run.poll_error}</div>}
            {run.sql_path && <div className="small muted">Script: <span className="mono">{run.sql_path}</span></div>}
          </div>}
        </Card>)}
      <div className="kpis">
        <Kpi v={`${loaded}/${d.items.length}`} l="bronze tables loaded" accent />
        <Kpi v={d.items.reduce((n: number, i: any) => n + (i.loaded_rows ?? 0), 0).toLocaleString()} l="rows found in bronze" />
        <Kpi v={d.warehouse?.kind === "local" ? "Local stand-in" : "SQL warehouse"} l={d.warehouse?.kind === "local" ? "DuckDB, for development" : d.warehouse?.host ?? ""} />
      </div>
      <Card title="SQL for the pipeline" flush actions={<>
        <button disabled={a.busy} onClick={() => st.reload()}>Check Unity Catalog again</button>
        {d.mode === "manual" && d.can_run_locally && <button disabled={a.busy} onClick={() => a.run(async () => { await post(`/sessions/${sid}/pipeline/run-locally`); st.reload(); refresh(); }, "SQL run on the local stand-in")}>
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
      {auto && !auto.ok && <div className="banner err" role="alert"><b>Phase 2 did not start by itself.</b><span>{auto.error} Open Harmonisation and run it from there.</span></div>}
      <div className="gate">
        <div><div className="strong">Pipeline load</div>
          <div className="small muted">{after ? `Confirmed by ${s.outputs?.pipeline?.confirmed_by ?? ""}${auto?.ok ? ". Phase 2 started automatically" + (auto.gate_b === "approved" ? " and passed Gate B (known layout)." : "; Gate B is waiting for review.") : ". Phase 2 is open."}`
            : d.mode === "job" ? (run?.state === "failed" ? "The job failed. Fix the cause and run it again." : "Waiting for the pipeline job.")
            : d.all_loaded ? "All tables loaded. Confirm to start harmonisation." : "Waiting for the pipeline run."}</div></div>
        {after ? <button className="primary" onClick={() => nav(`/s/${sid}/${s.stage === "done" ? "signoff" : s.stage}`)}>Continue to {s.stage === "harmonisation" ? "Harmonisation" : s.stage === "validate" ? "Validate" : "Sign off"}</button>
          : d.mode === "manual" ? <button className="primary" disabled={!d.all_loaded || a.busy} onClick={() => a.run(async () => { await post(`/sessions/${sid}/pipeline/confirm`, {}); refresh(); nav(`/s/${sid}/harmonisation`); }, "Load confirmed: Phase 2 open")}>
            Confirm load and start Phase 2</button> : null}
      </div>
    </>
  );
}
