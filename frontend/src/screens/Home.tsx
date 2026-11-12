import { useState } from "react";
import { get, post } from "../api";
import { Card, ErrorBanner, fmtTime, Kpi, Rag, useAction, useLoad } from "../components/ui";
import type { Nav } from "../App";

export function Home({ nav, sessions }: { nav: Nav; sessions: any[] }) {
  const lib = useLoad(() => get<any[]>("/library/mappings"), []);
  const cw = useLoad(() => get<Record<string, { accepted: number; rejected: number }>>("/library/crosswalk"), []);
  const learned = Object.values(cw.data ?? {}).reduce((a, x) => a + x.accepted, 0);
  const env = useLoad(() => get<any>("/admin/environment"), []);
  const inbox = useLoad(() => get<any[]>("/inbox"), []);
  const seed = useAction();
  const start = useAction();
  const uc = useAction();
  const [check, setCheck] = useState<any>(null);
  const waiting = (inbox.data ?? []).filter((f) => !f.sessions.length);
  const done = (inbox.data ?? []).filter((f) => f.sessions.length);
  return (
    <>
      <div className="banner info">
        <div><b>From retailer files to bronze-ready output.</b> AI suggests, people decide at three gates, and every decision is traced.
          The first file of a layout is reviewed; repeats reuse the approved mapping. Target: onboarding from 10 days to 2–3 days.</div>
      </div>
      {env.data && (
        <Card title="Environment" actions={<>
          <button disabled={uc.busy} onClick={() => uc.run(async () => setCheck(await get<any>("/admin/check-uc")))}>
            {uc.busy ? <><span className="spin" /> Checking…</> : "Check Unity Catalog"}</button>
          <button disabled={seed.busy} onClick={() => seed.run(async () => { await post("/admin/seed"); env.reload(); inbox.reload(); }, "Demo data loaded")}>
          {seed.busy ? <><span className="spin" /> Loading…</> : env.data.landing_files ? "Reload demo data" : "Load demo data"}</button></>}>
          <div className="grid2 small">
            <div><b>Files (ADLS stand-in)</b>: {env.data.storage} · <span className="mono">{env.data.storage_root}</span></div>
            <div><b>Tables</b>: {env.data.warehouse === "local" ? "local DuckDB stand-in" : "Unity Catalog via SQL warehouse"} · bronze <span className="mono">{env.data.bronze}</span>, silver <span className="mono">{env.data.silver}</span></div>
            <div><b>Mapping JSON</b>: Delta format ({env.data.mapping_store.mode}) · <span className="mono">{env.data.mapping_store.location ?? env.data.mapping_store.error}</span></div>
            <div><b>Landing zone</b>: {env.data.landing_files} file(s) · <b>AI</b>: {env.data.llm} · <b>Pipeline</b>: {env.data.pipeline_mode === "job" ? "job started by the app" : env.data.pipeline_mode === "watch_table" ? "Reckitt runs it; app watches bronze" : env.data.pipeline_run_allowed ? "manual, simulation allowed" : "manual (Reckitt runs it)"}</div>
            <div><b>RB codes</b>: {env.data.mdm_source === "uc" ? <>Unity Catalog <span className="mono">{env.data.mdm_tables?.join(", ")}</span></> : "CSV extracts in storage"}</div>
            <div><b>Audit log</b>: {env.data.audit_table ? <span className="mono">{env.data.audit_table}</span> : "session files only (local)"}</div>
          </div>
          {check && <div className="stack" style={{ marginTop: 12 }}>
            <div className="small">{check.ok ? <span className="rag green">Connected</span> : <span className="rag red">Problem</span>} as <b>{check.identity ?? "–"}</b> · {check.auth}
              {check.warehouse?.http_path && <> · <span className="mono">{check.warehouse.http_path}</span></>}{check.error && <> · {check.error}</>}</div>
            <table className="t"><thead><tr><th>Table</th><th>Status</th><th>Rows</th><th>Time</th></tr></thead>
              <tbody>{check.tables.map((t: any) => (
                <tr key={t.name}><td><b>{t.name}</b><div className="mono small muted">{t.fqn}</div></td>
                  <td>{t.status === "ok" ? <Rag r="green" label="readable" /> : t.status === "missing" ? <Rag r="amber" label="not created yet" /> : <Rag r="red" label="error" />}
                    {t.error && <div className="small muted">{t.error}</div>}</td>
                  <td>{t.rows ?? "–"}</td><td className="muted">{t.ms} ms</td></tr>))}</tbody></table>
          </div>}
          <ErrorBanner error={seed.error ?? uc.error} />
        </Card>)}
      <Card title={`New files waiting (${waiting.length})`} flush actions={<button onClick={() => inbox.reload()}>Refresh</button>}>
        {waiting.length === 0 ? <div className="empty">No new files in the landing zone. Files dropped under <span className="mono">landing/&lt;market&gt;/&lt;retailer&gt;/</span> show up here.</div> :
          <table className="t"><thead><tr><th>File</th><th>Retailer</th><th>Arrived</th><th></th></tr></thead>
            <tbody>{waiting.map((f) => (
              <tr key={f.key}><td><b>{f.name}</b><div className="mono small muted">{f.path}</div></td>
                <td>{f.retailer ?? "–"} · {f.market ?? "–"}</td>
                <td className="small">{f.arrived_at ? fmtTime(f.arrived_at) : "–"}{f.recorded_by_job && <div className="muted">recorded by intake job</div>}</td>
                <td style={{ textAlign: "right" }}><button className="primary small" disabled={start.busy || !f.retailer}
                  onClick={() => start.run(async () => { const r = await post<any>("/inbox/start", { key: f.key }); nav(`/s/${r.session_id}/${r.stage === "pipeline" ? "pipeline" : "discovery"}`); }, "Onboarding started: AI Discovery and mapping done")}>
                  {start.busy ? <><span className="spin" /> Analysing…</> : "Start onboarding"}</button></td></tr>))}</tbody></table>}
        {done.length > 0 && <div className="small muted" style={{ padding: "8px 14px" }}>{done.length} other file(s) in the landing zone are already onboarded: {done.map((f) => f.name).join(", ")}.</div>}
        <ErrorBanner error={start.error} />
      </Card>
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
