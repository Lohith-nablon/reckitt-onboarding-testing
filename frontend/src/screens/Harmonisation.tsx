import { Fragment, useState } from "react";
import { get, post } from "../api";
import { By, Card, Conf, ErrorBanner, GateBar, Kpi, Loading, useAction, useLoad } from "../components/ui";
import type { ScreenProps } from "../App";

const METHOD: Record<string, string> = {
  crosswalk: "learned earlier", universal_code: "EAN in MDM", description_attributes: "description match",
  address: "address match", llm_adjudicated: "AI confirmed", none: "no match",
};
const DECISION: Record<string, string> = {
  crosswalk: "From crosswalk", auto_accepted: "Auto-accepted", accepted: "Accepted", chosen: "Changed by reviewer",
  rejected: "Rejected", left_empty: "Left empty",
};

export function HarmonisationScreen({ ov, refresh, nav }: ScreenProps) {
  const s = ov.session;
  const sid = s.session_id;
  const h = useLoad(() => get<any>(`/sessions/${sid}/harmonisation`), [sid, s.version]);
  const [tab, setTab] = useState<"codes" | "values">("codes");
  const run = useAction();
  const gate = useAction();
  const phase2 = ["harmonisation", "validate", "signoff", "done"].includes(s.stage);
  if (!phase2) return <div className="banner">Phase 2 starts once Reckitt’s pipeline has loaded the tables. Confirm the load on the Pipeline screen first.</div>;
  if (!h.data) return <Loading />;
  const started = h.data.started;
  const summ = h.data.codes_summary as Record<string, any>;
  const pending = Object.values(summ).reduce((n: number, e: any) => n + (e.pending ?? 0), 0);
  const ruleIssues = (h.data.value_rules as any[]).filter((r) => r.status === "proposed").length;
  return (
    <>
      <GateBar name="B" label="harmonisation review" gate={s.gates.B}>
        <button disabled={run.busy} onClick={() => run.run(async () => { await post(`/sessions/${sid}/harmonisation/run`); h.reload(); refresh(); }, "Codes matched")}>
          {run.busy ? <><span className="spin" /> Matching…</> : started ? "Re-run matching" : "Run code matching"}</button>
        {s.gates.B.status === "approved"
          ? <button className="primary" onClick={() => nav(`/s/${sid}/validate`)}>Continue to Validate</button>
          : <button className="primary" disabled={!started || gate.busy} onClick={() => gate.run(async () => { await post(`/sessions/${sid}/gates/B/approve`, {}); refresh(); }, "Gate B approved; decisions saved to the crosswalk")}>
            Approve harmonisation</button>}
      </GateBar>
      <ErrorBanner error={run.error || gate.error} />
      <div className="small muted">Reading the loaded tables from Unity Catalog: {(h.data.bronze as any[]).map((b) => `${b.fqn} (${b.rows} rows)`).join(" · ")}</div>
      {started && <div className="kpis">
        {Object.entries(summ).map(([ent, e]: [string, any]) => <Kpi key={ent} v={`${e.total - e.pending}/${e.total}`} l={`${ent} codes resolved`} />)}
        {Object.entries(summ).map(([ent, e]: [string, any]) => <Kpi key={ent + "c"} v={e.crosswalk} l={`${ent} codes reused from earlier decisions`} />)}
        <Kpi v={pending} l="codes waiting for you" accent />
        <Kpi v={ruleIssues} l="value rules to confirm" accent />
      </div>}
      <div className="tabs">
        <button className={tab === "codes" ? "on" : ""} onClick={() => setTab("codes")}>Codes (M4)</button>
        <button className={tab === "values" ? "on" : ""} onClick={() => setTab("values")}>Value standards (M5)</button>
      </div>
      {tab === "codes" ? (started ? <Codes sid={sid} rows={h.data.codes} reload={() => { h.reload(); refresh(); }} />
        : <div className="empty">Run code matching to resolve retailer product and store codes to RB codes.</div>)
        : <Standards sid={sid} rules={h.data.value_rules} reload={() => { h.reload(); refresh(); }} />}
    </>
  );
}

function Codes({ sid, rows, reload }: { sid: string; rows: any[]; reload: () => void }) {
  const [filter, setFilter] = useState<"open" | "all">("open");
  const [open, setOpen] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const a = useAction();
  const shown = rows.filter((r) => filter === "all" || (["needs_review", "unmatched"].includes(r.status) && !r.decision));
  const search = useLoad(() => open && q.length > 1 ? get<any[]>(`/mdm/search?entity=${rows.find((r) => r.match_id === open)?.entity}&q=${encodeURIComponent(q)}`) : Promise.resolve([]), [open, q]);
  const decide = (id: string, action: string, rb_code?: string) =>
    a.run(async () => { await post(`/sessions/${sid}/harmonisation/codes/${encodeURIComponent(id)}`, { action, rb_code: rb_code ?? null }); setOpen(null); reload(); }, "Decision saved");
  return (
    <Card title="Retailer code → RB code" flush actions={<>
      <select style={{ width: 170 }} value={filter} onChange={(e) => setFilter(e.target.value as any)}><option value="open">Waiting for a decision</option><option value="all">All codes</option></select>
      <button className="small" disabled={a.busy} onClick={() => a.run(async () => { const r = await post<{ accepted: number }>(`/sessions/${sid}/harmonisation/codes/bulk-accept`, { min_confidence: 0.8 }); reload(); return r; }, "Accepted all proposals at 80% or more")}>Accept proposals ≥ 80%</button>
    </>}>
      <ErrorBanner error={a.error} />
      {shown.length === 0 ? <div className="empty">{filter === "open" ? "Nothing waiting. Every code is resolved, decided or left for later." : "No codes."}</div> :
        <div className="scroll" style={{ maxHeight: 620 }}><table className="t"><thead><tr><th>Retailer code</th><th>Description</th><th>Proposed RB code</th><th>How</th><th>Status</th><th>Decision</th></tr></thead>
          <tbody>{shown.map((r) => (
            <Fragment key={r.match_id}>
              <tr className={open === r.match_id ? "sel" : ""}>
                <td className="mono">{r.source_key}<div className="small muted">{r.entity}</div></td>
                <td style={{ maxWidth: 260 }}>{r.source_label}{r.source_attrs?.ean && <div className="small muted mono">EAN {r.source_attrs.ean}</div>}</td>
                <td className="mono">{r.final_code ?? r.rb_code ?? "–"}<div><Conf v={r.confidence} /></div></td>
                <td className="small">{METHOD[r.method] ?? r.method}<div className="muted">{r.reason}</div></td>
                <td>{r.status === "needs_review" ? <span className="tag flag">check</span> : r.status === "unmatched" ? <span className="tag dashed">no match</span> : <span className="tag outline-blue">accepted</span>}</td>
                <td>{r.decision ? <div className="small">{DECISION[r.decision] ?? r.decision}<div><By who={r.decided_by} /></div></div> :
                  <div className="row">{r.rb_code && <button className="small primary" disabled={a.busy} onClick={() => decide(r.match_id, "accept")}>Accept</button>}
                    <button className="small" onClick={() => { setOpen(open === r.match_id ? null : r.match_id); setQ(""); }}>Options ({r.candidates.length})</button></div>}</td>
              </tr>
              {open === r.match_id && (
                <tr><td colSpan={6} style={{ background: "var(--salt-soft)" }}>
                  <div className="stack">
                    <b className="small">Candidates from MDM</b>
                    {r.candidates.map((c: any) => (
                      <div className="row" key={c.code}><span className="mono" style={{ width: 120 }}>{c.code}</span><span className="grow">{c.label}</span><Conf v={c.score} />
                        <button className="small" disabled={a.busy} onClick={() => decide(r.match_id, "choose", c.code)}>Use this</button></div>))}
                    {r.candidates.length === 0 && <div className="small muted">No candidates scored.</div>}
                    <div className="row"><input style={{ maxWidth: 320 }} placeholder="Search MDM by code or description" value={q} onChange={(e) => setQ(e.target.value)} /></div>
                    {(search.data ?? []).map((m) => <div className="row" key={m.code}><span className="mono" style={{ width: 120 }}>{m.code}</span><span className="grow">{m.label}</span>
                      <button className="small" onClick={() => decide(r.match_id, "choose", m.code)}>Use this</button></div>)}
                    <div className="row"><button className="small" onClick={() => decide(r.match_id, "reject")}>Not an RB product/store</button>
                      <button className="small" onClick={() => decide(r.match_id, "leave_empty")}>Leave empty for now</button></div>
                  </div>
                </td></tr>)}
            </Fragment>))}</tbody></table></div>}
    </Card>
  );
}

function Standards({ sid, rules, reload }: { sid: string; rules: any[]; reload: () => void }) {
  const [edit, setEdit] = useState<Record<string, Record<string, string>>>({});
  const a = useAction();
  const save = (r: any) => a.run(async () => {
    await post(`/sessions/${sid}/harmonisation/standards/${r.rule_id}`, { mapping: edit[r.rule_id] ?? null }); setEdit({}); reload();
  }, "Value standard saved");
  return (
    <Card title="Value standards in silver" flush>
      <p className="small muted" style={{ padding: "10px 16px 0" }}>The same thing written differently by retailers becomes one standard value in silver (EA, GBP, Y/N). Values found in the loaded tables, with the proposed standard; type a standard for any it does not know.</p>
      <ErrorBanner error={a.error} />
      {rules.length === 0 ? <div className="empty">No unit, currency or flag columns in the loaded tables.</div> :
        <div className="stack" style={{ padding: 16 }}>{rules.map((r) => (
          <div key={r.rule_id} className="card">
            <div className="hd"><div><b className="mono">{r.target_table} · {r.column}</b> <span className="tag">{r.kind}</span></div>
              <div className="row small">{r.status === "proposed" ? <span className="tag flag">check</span> : <span>{r.status.replace("_", " ")} <By who={r.decided_by} /></span>}
                <button className="small primary" disabled={a.busy} onClick={() => save(r)}>{edit[r.rule_id] ? "Save" : "Approve"}</button></div></div>
            <div className="bd flush"><table className="t"><thead><tr><th>Value in bronze</th><th>Rows</th><th>Standard in silver</th></tr></thead>
              <tbody>{r.values.map((v: any) => (
                <tr key={v.value}><td className="mono">{v.value}</td><td>{v.rows}</td>
                  <td><input style={{ maxWidth: 160 }} value={edit[r.rule_id]?.[v.value] ?? v.standard ?? ""} placeholder="no standard"
                    onChange={(e) => setEdit({ ...edit, [r.rule_id]: { ...(edit[r.rule_id] ?? {}), [v.value]: e.target.value } })} /></td></tr>))}</tbody></table></div>
          </div>))}</div>}
    </Card>
  );
}
