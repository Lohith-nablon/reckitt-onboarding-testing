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
  if (s.gates.A.status !== "approved") return <div className="banner">Approve the mappings (Gate A) first: harmonisation runs on approved mappings only.</div>;
  if (!h.data) return <Loading />;
  const started = s.gates.B.status !== "not_started";
  const summ = h.data.codes_summary as Record<string, any>;
  const pending = Object.values(summ).reduce((n: number, e: any) => n + (e.pending ?? 0), 0);
  const ruleIssues = (h.data.value_rules as any[]).filter((r) => r.issue && r.status === "proposed").length;
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
      {started && <div className="kpis">
        {Object.entries(summ).map(([ent, e]: [string, any]) => <Kpi key={ent} v={`${e.total - e.pending}/${e.total}`} l={`${ent} codes resolved`} />)}
        {Object.entries(summ).map(([ent, e]: [string, any]) => <Kpi key={ent + "c"} v={e.crosswalk} l={`${ent} codes reused from earlier decisions`} />)}
        <Kpi v={pending} l="codes waiting for you" accent />
        <Kpi v={ruleIssues} l="value rules to confirm" accent />
      </div>}
      <div className="tabs">
        <button className={tab === "codes" ? "on" : ""} onClick={() => setTab("codes")}>Codes (M4)</button>
        <button className={tab === "values" ? "on" : ""} onClick={() => setTab("values")}>Values (M5)</button>
      </div>
      {tab === "codes" ? (started ? <Codes sid={sid} rows={h.data.codes} reload={() => { h.reload(); refresh(); }} />
        : <div className="empty">Run code matching to resolve retailer product and store codes to RB codes.</div>)
        : <Values sid={sid} rules={h.data.value_rules} reload={() => { h.reload(); refresh(); }} />}
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

const KIND: Record<string, string> = { date_format: "Date format", number_format: "Numbers", code_suffix: "Code suffix", currency: "Currency", unit: "Unit of measure" };

function Values({ sid, rules, reload }: { sid: string; rules: any[]; reload: () => void }) {
  const [edit, setEdit] = useState<Record<string, string>>({});
  const a = useAction();
  const decide = (r: any, value?: string) => a.run(async () => {
    await post(`/sessions/${sid}/harmonisation/rules/${r.rule_id}`, { approve: true, value: value ?? null }); setEdit({}); reload();
  }, value ? "Rule changed" : "Rule approved");
  return (
    <Card title="Value rules" flush>
      <p className="small muted" style={{ padding: "10px 16px 0" }}>How values are converted so every retailer means the same thing. Changing a date format or suffix updates the mapping.</p>
      <ErrorBanner error={a.error} />
      {rules.length === 0 ? <div className="empty">No conversions needed.</div> :
        <table className="t"><thead><tr><th>Rule</th><th>Column</th><th>From → to</th><th>Examples</th><th>Check</th><th>Decision</th></tr></thead>
          <tbody>{rules.map((r) => (
            <tr key={r.rule_id} className={r.issue && r.status === "proposed" ? "sel" : ""}>
              <td><b>{KIND[r.kind] ?? r.kind}</b><div className="small muted">{r.file_name}</div></td>
              <td className="mono small">{r.target_column}{r.source_column && <div className="muted">from {r.source_column}</div>}</td>
              <td className="small">{r.from ?? "–"} → <b>{r.to}</b></td>
              <td className="small mono">{r.examples.slice(0, 3).map((e: any, i: number) => <div key={i}>{e.before} → {e.after}</div>)}</td>
              <td className="small">{r.parse_rate != null && <Conf v={r.parse_rate} />}{r.issue ? <div><span className="tag flag">check</span> {r.issue}</div> : <div className="muted">ok</div>}</td>
              <td className="small">
                {r.status !== "proposed" ? <div>{r.status} <By who={r.decided_by} /></div> : null}
                {r.editable && <div className="row" style={{ marginTop: 4 }}>
                  {r.kind === "number_format"
                    ? <select style={{ width: 120 }} value={edit[r.rule_id] ?? r.value ?? "."} onChange={(e) => setEdit({ ...edit, [r.rule_id]: e.target.value })}>
                      <option value=".">decimal "."</option><option value=",">decimal ","</option></select>
                    : <input style={{ width: 130 }} value={edit[r.rule_id] ?? (r.editable === "from" ? r.from : r.value) ?? ""} onChange={(e) => setEdit({ ...edit, [r.rule_id]: e.target.value })} />}
                  {edit[r.rule_id] !== undefined && <button className="small" disabled={a.busy} onClick={() => decide(r, edit[r.rule_id])}>Save</button>}
                </div>}
                {r.status === "proposed" && <button className="small primary" style={{ marginTop: 4 }} disabled={a.busy} onClick={() => decide(r)}>Approve</button>}
              </td>
            </tr>))}</tbody></table>}
    </Card>
  );
}
