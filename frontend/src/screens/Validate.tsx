import { useState } from "react";
import { get, post } from "../api";
import { Card, ErrorBanner, Kpi, Loading, useAction, useLoad } from "../components/ui";
import type { ScreenProps } from "../App";

export function ValidateScreen({ ov, refresh, nav }: ScreenProps) {
  const s = ov.session;
  const sid = s.session_id;
  const reps = useLoad(() => get<any[]>(`/sessions/${sid}/validate`), [sid, s.version]);
  const [sel, setSel] = useState(0);
  const a = useAction();
  if (s.gates.B.status !== "approved") return <div className="banner">Approve harmonisation (Gate B) first.</div>;
  if (!reps.data) return <Loading />;
  const r = reps.data[sel];
  const blocking = reps.data.reduce((n, x) => n + x.blocking.length, 0);
  return (
    <>
      <div className="row">
        <button className={reps.data.length ? "" : "primary"} disabled={a.busy} onClick={() => a.run(async () => { await post(`/sessions/${sid}/validate`); reps.reload(); refresh(); }, "Validation finished")}>
          {a.busy ? <><span className="spin" /> Building rows and checking…</> : reps.data.length ? "Validate again" : "Build and validate"}</button>
        <span className="grow" />
        {reps.data.length > 0 && <button className="primary" disabled={blocking > 0} onClick={() => nav(`/s/${sid}/signoff`)}>Continue to Sign off</button>}
      </div>
      <ErrorBanner error={a.error} />
      {reps.data.length === 0 ? <div className="empty">Builds the silver rows from the loaded bronze rows, the RB codes and the value standards, and runs the checks. Nothing is written until sign-off.</div> : <>
        <div className="row">{reps.data.map((x, i) => <button key={x.mapping_id} className={i === sel ? "primary" : ""} onClick={() => setSel(i)}>
          {x.target_table} <span className="small">· {x.file_name}{x.blocking.length ? ` · ${x.blocking.length} blocking` : ""}</span></button>)}</div>
        {r && <>
          {r.error && <ErrorBanner error={r.error} />}
          <div className="small muted">Built from <span className="mono">{r.bronze_fqn}</span> ({r.bronze_rows} rows loaded by the pipeline) · will be written to <span className="mono">{r.silver_fqn}</span> at sign-off</div>
          <div className="kpis">
            <Kpi v={r.rows.toLocaleString()} l="silver rows built" />
            <Kpi v={r.blocking.length} l="blocking checks" accent={r.blocking.length > 0} />
            <Kpi v={r.warnings?.length ?? 0} l="warnings" />
            <Kpi v={r.veritas_compare ? `${Math.round(r.veritas_compare.match_rate * 100)}%` : "n/a"} l="same as Veritas mapping" accent />
          </div>
          <Card title="Checks" flush>
            <table className="t"><thead><tr><th>Check</th><th>Severity</th><th>Result</th><th>Rows failing</th><th>Detail</th></tr></thead>
              <tbody>{r.checks.map((c: any) => (
                <tr key={c.check} className={!c.passed && c.severity === "error" ? "sel" : ""}>
                  <td className="mono small">{c.check}</td>
                  <td>{c.severity === "error" ? <span className="tag solid">blocks sign-off</span> : <span className="tag">warning</span>}</td>
                  <td>{c.passed ? "Passed" : <b>Failed</b>}</td>
                  <td>{c.failed_rows ? `${c.failed_rows.toLocaleString()} of ${c.total_rows.toLocaleString()}` : "–"}</td>
                  <td className="small">{c.detail}{c.sample?.length > 0 && <div className="mono muted">{JSON.stringify(c.sample[0])}</div>}</td>
                </tr>))}</tbody></table>
          </Card>
          {r.veritas_compare && (
            <Card title={`Veritas compare · #${r.veritas_compare.reference_id} ${r.veritas_compare.reference_name}`} flush>
              <div style={{ padding: "10px 16px" }} className="small">
                {r.veritas_compare.columns_matching} of {r.veritas_compare.columns_compared} target columns are mapped the same way as in Veritas.
                File type: {r.veritas_compare.file_type.map((f: any) => `${f.field} ${f.match ? "same" : `${f.ours} vs ${f.veritas}`}`).join(" · ")}
              </div>
              <div className="scroll" style={{ maxHeight: 360 }}><table className="t"><thead><tr><th>Column</th><th>Same</th><th>This tool</th><th>Veritas</th></tr></thead>
                <tbody>{r.veritas_compare.columns.map((c: any) => (
                  <tr key={c.column} className={c.match ? "" : "sel"}><td className="mono small">{c.column}</td><td>{c.match ? "Yes" : <b>No</b>}</td>
                    <td className="mono small">{c.ours}</td><td className="mono small">{c.veritas}</td></tr>))}</tbody></table></div>
            </Card>)}
          {!r.veritas_compare && <div className="banner">No Veritas mapping for this retailer and dataset to compare with{r.target_table.includes("pos_sellout") ? "" : " (this table is not a Veritas dataset)"}.</div>}
          <Card title={`Preview · first ${r.preview.rows.length} rows`} flush>
            <div className="scroll rows"><table className="t"><thead><tr>{r.preview.columns.map((c: string) => <th key={c}>{c}</th>)}</tr></thead>
              <tbody>{r.preview.rows.map((row: any[], i: number) => <tr key={i}>{row.map((v, j) => <td key={j} className="small">{v ?? <span className="muted">null</span>}</td>)}</tr>)}</tbody></table></div>
          </Card>
        </>}
      </>}
    </>
  );
}
