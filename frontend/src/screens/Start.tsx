import { useMemo, useState } from "react";
import { get, post, put, Session, upload } from "../api";
import { Card, ErrorBanner, Loading, useAction, useLoad } from "../components/ui";
import type { Nav, ScreenProps } from "../App";

export function NewSession({ nav }: { nav: Nav }) {
  const markets = useLoad(() => get<any[]>("/markets"), []);
  const [market, setMarket] = useState("UK");
  const [retailer, setRetailer] = useState("");
  const [name, setName] = useState("");
  const a = useAction();
  const m = markets.data?.find((x) => x.code === market);
  return (
    <Card title="Who is this onboarding for?">
      <div className="stack" style={{ maxWidth: 560 }}>
        <p className="muted">One session covers one retailer feed: its files, its mapping and every decision made on it.</p>
        <label className="f"><span><b>Market</b></span>
          <select value={market} onChange={(e) => { setMarket(e.target.value); setRetailer(""); }}>
            {(markets.data ?? []).map((x) => <option key={x.code} value={x.code}>{x.code} · {x.currency}</option>)}
          </select></label>
        <label className="f"><span><b>Retailer</b> (pick one or type a new retailer code)</span>
          <input list="retailers" value={retailer} onChange={(e) => setRetailer(e.target.value.toUpperCase())} placeholder="e.g. TESCO" />
          <datalist id="retailers">{(m?.retailers ?? []).map((r: any) => <option key={r.code} value={r.code}>{r.display_name}</option>)}</datalist></label>
        <label className="f"><span><b>Session name</b> (optional)</span>
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Tesco daily store sales" /></label>
        <ErrorBanner error={a.error} />
        <div><button className="primary" disabled={!retailer || a.busy} onClick={() => a.run(async () => {
          const s = await post<Session>("/sessions", { name, market, retailer });
          nav(`/s/${s.session_id}/start`);
        })}>Start onboarding</button></div>
      </div>
    </Card>
  );
}

export function StartScreen({ ov, refresh, nav }: ScreenProps) {
  const sid = ov.session.session_id;
  const form = useLoad(() => get<any>(`/sessions/${sid}/form`), [sid]);
  const target = useLoad(() => get<any>(`/sessions/${sid}/target`), [sid, ov.session.version]);
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [audience, setAudience] = useState<"all" | "internal" | "client">("all");
  const a = useAction();
  const t = useAction();
  const fields: any[] = form.data?.fields ?? [];
  const groups: any[] = form.data?.groups ?? [];
  const shown = useMemo(() => fields.filter((f) => audience === "all" || f.audience === audience), [fields, audience]);
  if (!form.data) return <Loading />;
  const val = (f: any) => edits[f.key] ?? f.value ?? "";
  const save = () => a.run(async () => {
    const r = await put<any>(`/sessions/${sid}/profile`, { values: edits });
    form.setData(r); setEdits({}); refresh();
  }, "Business fields saved");
  const c = form.data.completeness;
  return (
    <>
      <Card title="Business fields" actions={<>
        <span className="small muted">{c.filled}/{c.total} filled · {c.missing_required.length} required missing</span>
        <select style={{ width: 170 }} value={audience} onChange={(e) => setAudience(e.target.value as any)}>
          <option value="all">All fields</option><option value="internal">Filled by Reckitt</option><option value="client">Asked of the retailer</option></select>
      </>}>
        <div className="stack">
          <p className="muted small">Details that never appear in retailer files (legal entity, code suffixes, contacts). They fill target columns
            directly. Suggestions come from existing Veritas mappings and market defaults: use one or type your own.</p>
          {groups.map((g) => {
            const fs = shown.filter((f) => f.group === g.id);
            if (!fs.length) return null;
            return (
              <div key={g.id} className="stack">
                <h4>{g.label}</h4>
                <div className="grid2">
                  {fs.map((f) => (
                    <label key={f.key} className="f">
                      <span><b>{f.label}</b>{f.required && " *"} {f.used_by?.length > 0 && <span className="tag" title={f.used_by.join(", ")}>fills {f.used_by.length} column{f.used_by.length > 1 ? "s" : ""}</span>}</span>
                      {f.type === "select" ? (
                        <select value={val(f)} onChange={(e) => setEdits({ ...edits, [f.key]: e.target.value })}>
                          <option value="">–</option>{f.options.map((o: string) => <option key={o}>{o}</option>)}</select>
                      ) : <input type={f.type === "date" ? "date" : f.type === "email" ? "email" : "text"} value={val(f)}
                        onChange={(e) => setEdits({ ...edits, [f.key]: e.target.value })} placeholder={f.help} />}
                      {f.suggestion && !val(f) && <span className="small">Suggested: <b>{f.suggestion.value}</b> <span className="muted">({f.suggestion.why})</span>{" "}
                        <button className="ghost small" onClick={(e) => { e.preventDefault(); setEdits({ ...edits, [f.key]: f.suggestion.value }); }}>Use</button></span>}
                      {f.provided_by && !edits[f.key] && <span className="small muted">set by {f.provided_by}</span>}
                    </label>
                  ))}
                </div>
              </div>
            );
          })}
          <ErrorBanner error={a.error} />
          <div className="row"><button className="primary" disabled={!Object.keys(edits).length || a.busy} onClick={save}>Save fields</button>
            <button onClick={() => nav(`/s/${sid}/files`)}>Continue to Add files</button></div>
        </div>
      </Card>
      <Card title="Mapping file (target schema)" actions={
        <label className="btn" style={{ display: "inline-block" }}>Upload mapping file
          <input type="file" hidden accept=".csv,.xlsx,.xls,.sql,.ddl,.json" onChange={(e) => {
            const f = e.target.files?.[0]; if (!f) return;
            t.run(async () => { await upload(`/sessions/${sid}/target`, "file", [f]); target.reload(); refresh(); }, "Target schema loaded");
          }} /></label>}>
        <div className="stack">
          <p className="small muted">The market's target columns. CSV/Excel with at least <code>table</code> and <code>column</code>, SQL DDL or JSON.
            {target.data?.source === "default" ? " None uploaded yet: the built-in POS_SELLOUT stand-in is used." : ` Using ${target.data?.file_name}.`}</p>
          <ErrorBanner error={t.error} />
          {(target.data?.tables ?? []).map((tb: any) => (
            <details key={tb.name}>
              <summary><b>{tb.name}</b> <span className="muted small">· {tb.kind} · {tb.columns.length} columns{tb.veritas_dataset && ` · Veritas ${tb.veritas_dataset}`}</span></summary>
              <div className="scroll" style={{ maxHeight: 260, marginTop: 8 }}>
                <table className="t"><thead><tr><th>Column</th><th>Type</th><th>Filled from</th><th>Rules</th></tr></thead>
                  <tbody>{tb.columns.map((c: any) => <tr key={c.name}><td className="mono">{c.name}</td><td>{c.dtype}</td><td>{c.field_source}</td>
                    <td>{c.pk && <span className="tag solid">key</span>} {c.mandatory && <span className="tag flag">mandatory</span>}</td></tr>)}</tbody></table>
              </div>
            </details>
          ))}
        </div>
      </Card>
    </>
  );
}
