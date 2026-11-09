import { useState } from "react";
import { get, post, put } from "../api";
import { By, Card, Conf, ErrorBanner, Kpi, Loading, useAction, useLoad } from "../components/ui";
import type { ScreenProps } from "../App";

const SRC: Record<string, string> = { static: "rules", fuzzy: "rules", pattern: "rules", llm: "AI", human: "person", none: "–" };

export function DiscoveryScreen({ ov, refresh, nav }: ScreenProps) {
  const s = ov.session;
  const sid = s.session_id;
  const [fid, setFid] = useState(s.files[0]?.file_id);
  const [onlyReview, setOnlyReview] = useState(false);
  const rep = useLoad(() => fid ? get<any>(`/sessions/${sid}/discovery/${fid}`) : Promise.resolve(null), [fid, s.version]);
  const types = useLoad(() => get<{ name: string; kind: string }[]>("/catalogue/semantic-types"), []);
  const a = useAction();
  const run = useAction();
  const [answers, setAnswers] = useState<Record<string, string>>({});
  if (!s.files.length) return <div className="empty">Add files first.</div>;
  if (rep.error) return (
    <Card title="AI Discovery has not run yet">
      <div className="stack"><ErrorBanner error={run.error} />
        <div><button className="primary" disabled={run.busy} onClick={() => run.run(async () => { await post(`/sessions/${sid}/analyse`); refresh(); })}>
          {run.busy ? <><span className="spin" /> Analysing…</> : "Run AI Discovery and mapping"}</button></div></div>
    </Card>);
  if (!rep.data) return <Loading />;
  const r = rep.data;
  const setType = (sheet: string | null, column: string, semantic_type: string | null) =>
    a.run(async () => { await put(`/sessions/${sid}/discovery/${fid}/columns`, { sheet, column, semantic_type }); rep.reload(); refresh(); }, `${column} set`);
  const openQ = r.open_questions.filter((q: any) => !q.answer);
  return (
    <>
      <div className="row">
        {s.files.map((f) => <button key={f.file_id} className={f.file_id === fid ? "primary" : ""} onClick={() => setFid(f.file_id)}>{f.name}</button>)}
        <span className="grow" />
        <span className="small muted">Analysed by <By who={r.decided_by} /> · {r.sample_rows_sent ? `${r.sample_rows_sent} sample rows sent to the AI` : "no rows sent to an LLM"}</span>
      </div>
      {r.reused_layout && <div className="banner info"><b>Known layout.</b> This file matches a signed-off mapping in the library, so discovery review is skipped. Shown for reference.</div>}
      {r.sheets.map((sh: any) => {
        const t = sh.table;
        const cols = onlyReview ? sh.columns.filter((c: any) => c.needs_review) : sh.columns;
        const primary = t.targets.find((x: any) => x.primary);
        return (
          <div className="stack" key={sh.sheet ?? "data"}>
            <Card title={<>What this {sh.sheet ? `sheet (${sh.sheet})` : "file"} is</>}>
              <div className="stack">
                <p style={{ fontSize: 15 }}>{t.description}</p>
                <div className="kpis">
                  <Kpi v={t.rows.toLocaleString()} l="rows" />
                  <Kpi v={t.columns} l="columns" />
                  <Kpi v={t.classification} l={`table type · ${Math.round(t.classification_confidence * 100)}%`} />
                  <Kpi v={sh.columns.filter((c: any) => c.needs_review).length} l="columns to check" accent />
                  <Kpi v={openQ.length} l="open questions" accent />
                </div>
                <div className="grid2 small">
                  <div><b>Feeds</b>: {t.targets.map((x: any) => `${x.target_table}${x.primary ? " (main)" : ""} ${Math.round(x.score * 100)}%`).join(" · ") || "no target recognised"}
                    {primary?.missing_mandatory?.length > 0 && <div>Missing for {primary.target_table}: {primary.missing_mandatory.join(", ")}</div>}</div>
                  <div><b>One row per</b>: {t.grain?.join(" + ") || "not found"}{t.period && <> · <b>period</b> {t.period}</>}</div>
                  <div><b>Dates</b>: {t.date_range ? `${t.date_range[0]} to ${t.date_range[1]}` : "–"}{t.date?.format && ` · format ${t.date.format}`}{t.date?.week_end_day && ` · falls on ${t.date.week_end_day}`}</div>
                  <div><b>Currency</b>: {t.currency ?? "–"} {t.currency_source && <span className="muted">({t.currency_source})</span>}</div>
                  <div><b>Header</b>: row {t.header_row} · {Math.round(t.header_confidence * 100)}% sure{t.footer_rows_dropped ? ` · ${t.footer_rows_dropped} footer rows dropped` : ""}</div>
                  <div><b>EAN valid</b>: {t.ean_valid_share == null ? "–" : `${Math.round(t.ean_valid_share * 100)}%`}</div>
                </div>
                {t.data_quality_notes?.length > 0 && <div className="small"><b>AI notes:</b> {t.data_quality_notes.join(" · ")}</div>}
              </div>
            </Card>
            <Card title="Columns" flush actions={<label className="small row"><input type="checkbox" style={{ width: "auto" }} checked={onlyReview} onChange={(e) => setOnlyReview(e.target.checked)} /> Only columns to check</label>}>
              <div className="scroll" style={{ maxHeight: 560 }}>
                <table className="t" style={{ minWidth: 1100, tableLayout: "fixed" }}><colgroup><col style={{ width: 150 }} /><col style={{ width: 250 }} /><col style={{ width: 190 }} /><col style={{ width: 90 }} /><col style={{ width: 110 }} /><col style={{ width: 100 }} /><col style={{ width: 150 }} /><col style={{ width: 60 }} /></colgroup><thead><tr><th>Column</th><th>What it holds</th><th>Type (you can change it)</th><th>Kind</th><th>Confidence</th><th>Format</th><th>Examples</th><th>Empty</th></tr></thead>
                  <tbody>{cols.map((c: any) => (
                    <tr key={c.column} className={c.needs_review ? "sel" : ""}>
                      <td style={{ overflowWrap: "anywhere" }}><b className="mono">{c.column}</b>{c.needs_review && <div><span className="tag flag">check</span></div>}</td>
                      <td>{c.meaning}<div className="small muted">{c.rationale}</div></td>
                      <td>
                        <select value={c.semantic_type ?? ""} disabled={a.busy} onChange={(e) => setType(sh.sheet, c.column, e.target.value || null)}>
                          <option value="">not used / unknown</option>
                          {(types.data ?? []).map((x) => <option key={x.name} value={x.name}>{x.name}</option>)}
                        </select>
                        <div className="row small" style={{ marginTop: 4 }}><span className="muted">by {SRC[c.source] ?? c.source}</span>
                          {c.needs_review && c.semantic_type && <button className="ghost small" onClick={() => setType(sh.sheet, c.column, c.semantic_type)}>Confirm</button>}</div>
                      </td>
                      <td>{c.archetype}</td>
                      <td style={{ whiteSpace: "nowrap" }}><Conf v={c.confidence} /></td>
                      <td className="small" style={{ whiteSpace: "nowrap" }}>{[c.date_format, c.currency, c.unit].filter(Boolean).join(" · ") || "–"}</td>
                      <td className="small mono" style={{ overflowWrap: "anywhere" }}>{c.examples.slice(0, 3).join(", ")}</td>
                      <td className="small">{c.null_rate == null ? "–" : `${Math.round(c.null_rate * 100)}%`}</td>
                    </tr>))}</tbody></table>
              </div>
            </Card>
          </div>
        );
      })}
      <ErrorBanner error={a.error} />
      <Card title={`Open questions (${openQ.length} open of ${r.open_questions.length})`}>
        {r.open_questions.length === 0 ? <div className="muted">Nothing to clarify.</div> :
          <div className="stack">{r.open_questions.map((q: any) => (
            <div key={q.id} className="row" style={{ alignItems: "flex-start", borderBottom: "1px solid var(--salt-soft)", paddingBottom: 8 }}>
              <span className="tag">{q.topic}</span>
              <div className="grow">
                <div>{q.question}</div>
                {q.proposed_answer && <div className="small muted">Suggested: {q.proposed_answer}{q.confidence != null && ` (${Math.round(q.confidence * 100)}%)`}</div>}
                {q.answer ? <div className="small">Answered by {q.answered_by}: <b>{q.answer}</b></div> :
                  <div className="row" style={{ marginTop: 6 }}>
                    <input style={{ maxWidth: 360 }} value={answers[q.id] ?? q.proposed_answer ?? ""} onChange={(e) => setAnswers({ ...answers, [q.id]: e.target.value })} />
                    <button className="small" disabled={a.busy} onClick={() => a.run(async () => { await post(`/sessions/${sid}/discovery/${fid}/questions/${q.id}`, { answer: answers[q.id] ?? q.proposed_answer ?? "" }); rep.reload(); refresh(); }, "Answer saved")}>Save answer</button>
                  </div>}
              </div>
            </div>))}</div>}
      </Card>
      <div className="row"><button className="primary" onClick={() => nav(`/s/${sid}/mapping`)}>Continue to Mapping</button>
        <span className="small muted">Changing a column type here reopens mapping approval, and mappings are re-proposed from the Mapping screen.</span></div>
    </>
  );
}
