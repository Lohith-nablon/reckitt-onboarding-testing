import { useState } from "react";
import { post } from "../api";
import { By, Card, Conf, ErrorBanner, useAction } from "../components/ui";

const KIND: Record<string, string> = { date_format: "Date format", number_format: "Numbers", code_suffix: "Code suffix", currency: "Currency", unit: "Unit of measure" };

export function Conversions({ sid, rules, reload }: { sid: string; rules: any[]; reload: () => void }) {
  const [edit, setEdit] = useState<Record<string, string>>({});
  const a = useAction();
  const decide = (r: any, value?: string) => a.run(async () => {
    await post(`/sessions/${sid}/conversions/${r.rule_id}`, { approve: true, value: value ?? null }); setEdit({}); reload();
  }, value ? "Rule changed" : "Rule approved");
  return (
    <Card title="Conversions in the SQL" flush>
      <p className="small muted" style={{ padding: "10px 16px 0" }}>How the generated SQL converts values while loading bronze: dates, numbers, code suffixes, currency and unit. Rules with a check need a decision before Gate A; changing one updates the mapping.</p>
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
