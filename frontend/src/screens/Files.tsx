import { useState } from "react";
import { del, get, post, upload } from "../api";
import { Card, ErrorBanner, useAction, useLoad } from "../components/ui";
import type { ScreenProps } from "../App";

export function FilesScreen({ ov, refresh, nav }: ScreenProps) {
  const s = ov.session;
  const sid = s.session_id;
  const [over, setOver] = useState(false);
  const [tab, setTab] = useState<"landing" | "upload">("landing");
  const [picked, setPicked] = useState<string[]>([]);
  const [preview, setPreview] = useState<string | null>(null);
  const [results, setResults] = useState<{ file: string; error: string }[]>([]);
  const landing = useLoad(() => get<{ key: string; name: string; path: string; uri: string }[]>("/landing"), []);
  const pv = useLoad(() => preview ? get<any>(`/sessions/${sid}/files/${preview}/preview?rows=15`) : Promise.resolve(null), [preview]);
  const a = useAction();
  const go = useAction();
  const add = (files: File[]) => a.run(async () => {
    const r = await upload<{ added: any[]; errors: any[] }>(`/sessions/${sid}/files`, "files", files);
    setResults(r.errors); refresh();
  }, "Files added");
  const locked = s.gates.A.status === "approved";
  return (
    <>
      <Card title="Add retailer files">
        <div className="stack">
          <div className="tabs">
            <button className={tab === "landing" ? "on" : ""} onClick={() => setTab("landing")}>Bronze landing zone (ADLS)</button>
            <button className={tab === "upload" ? "on" : ""} onClick={() => setTab("upload")}>Upload</button>
          </div>
          {tab === "landing" && (
            <div className="stack">
              <p className="small muted">Raw retailer files as they arrive in the bronze landing zone. Pick the ones for this onboarding.</p>
              {(landing.data ?? []).length === 0 && <div className="muted">No files in the landing zone. Run <code>python -m onboarding_poc seed</code> for demo files.</div>}
              {(landing.data ?? []).map((x) => (
                <label className="row small" key={x.key} style={{ cursor: "pointer" }}>
                  <input type="checkbox" style={{ width: "auto" }} checked={picked.includes(x.key)}
                    onChange={(e) => setPicked(e.target.checked ? [...picked, x.key] : picked.filter((k) => k !== x.key))} />
                  <span className="mono grow">{x.path}</span><span className="muted" style={{ wordBreak: "break-all" }}>{x.uri}</span>
                </label>
              ))}
              <div><button className="primary" disabled={!picked.length || a.busy} onClick={() => a.run(async () => {
                await post(`/sessions/${sid}/files/landing`, { keys: picked }); setPicked([]); refresh();
              }, "Files added from the landing zone")}>Add {picked.length || ""} selected</button></div>
            </div>
          )}
          {tab === "upload" && (
            <label className={`drop ${over ? "over" : ""}`} onDragOver={(e) => { e.preventDefault(); setOver(true); }} onDragLeave={() => setOver(false)}
              onDrop={(e) => { e.preventDefault(); setOver(false); add(Array.from(e.dataTransfer.files)); }}>
              <input type="file" multiple hidden accept=".csv,.txt,.tsv,.xlsx,.xls,.xlsm" onChange={(e) => add(Array.from(e.target.files ?? []))} />
              <b>Drop files here or click to choose</b><div className="small">CSV, TXT, TSV or Excel. Each sheet is read separately; header rows and footers are found automatically.</div>
            </label>
          )}
          {a.busy && <div className="small"><span className="spin" /> Reading files…</div>}
          <ErrorBanner error={a.error} />
          {results.map((r) => <ErrorBanner key={r.file} error={`${r.file}: ${r.error}`} />)}
        </div>
      </Card>

      <Card title={`Files in this session (${s.files.length})`} flush actions={
        <button className="primary" disabled={!s.files.length || go.busy} onClick={() => go.run(async () => { const r = await post<any>(`/sessions/${sid}/analyse`); refresh(); nav(`/s/${sid}/${r.published ? "pipeline" : "discovery"}`); })}>
          {go.busy ? <><span className="spin" /> Analysing…</> : "Run AI Discovery and mapping"}</button>}>
        {go.error && <div style={{ padding: 12 }}><ErrorBanner error={go.error} /></div>}
        {s.files.length === 0 ? <div className="empty">No files yet.</div> :
          <table className="t"><thead><tr><th>File</th><th>Sheets</th><th>Rows</th><th>Layout</th><th>Source</th><th /></tr></thead>
            <tbody>{s.files.map((f) => (
              <tr key={f.file_id} className={preview === f.file_id ? "sel" : ""}>
                <td><b>{f.name}</b><div className="small muted">{(f.size_bytes / 1024).toFixed(0)} KB · added by {f.added_by}</div></td>
                <td>{f.sheets.map((x) => <div key={x.fingerprint} className="small">{x.sheet ?? "data"} · {x.columns.length} cols · header row {x.header_row + 1}{x.delimiter ? ` · '${x.delimiter === "\t" ? "tab" : x.delimiter}'` : ""}</div>)}</td>
                <td>{f.sheets.reduce((n, x) => n + x.rows, 0).toLocaleString()}</td>
                <td>{f.known_layout ? <span className="tag solid" title={f.sheets.map((x) => x.library_hit).join(", ")}>Known · skips review</span> : <span className="tag dashed">New layout</span>}</td>
                <td className="small muted" style={{ wordBreak: "break-all" }}>{f.source}</td>
                <td className="row"><button className="small" onClick={() => setPreview(preview === f.file_id ? null : f.file_id)}>{preview === f.file_id ? "Hide" : "Preview"}</button>
                  <button className="small" disabled={locked} title={locked ? "Reopen Gate A first" : ""} onClick={() => a.run(async () => { await del(`/sessions/${sid}/files/${f.file_id}`); if (preview === f.file_id) setPreview(null); refresh(); }, "File removed")}>Remove</button></td>
              </tr>))}</tbody></table>}
      </Card>

      {preview && pv.data && pv.data.sheets.map((sh: any) => (
        <Card key={sh.sheet ?? "data"} title={`Preview · ${sh.sheet ?? "data"} · ${sh.total_rows.toLocaleString()} rows`} flush>
          {sh.title_rows?.length > 0 && <div className="small muted" style={{ padding: "8px 12px" }}>Title rows above the header: {sh.title_rows.join(" | ")}</div>}
          <div className="scroll rows"><table className="t"><thead><tr>{sh.columns.map((c: string) => <th key={c}>{c}</th>)}</tr></thead>
            <tbody>{sh.rows.map((r: any[], i: number) => <tr key={i}>{r.map((v, j) => <td key={j}>{v}</td>)}</tr>)}</tbody></table></div>
        </Card>
      ))}
    </>
  );
}
