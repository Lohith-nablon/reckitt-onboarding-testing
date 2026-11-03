import { useState } from "react";
import { del, get, post, upload } from "../api";
import { Card, ErrorBanner, useAction, useLoad } from "../components/ui";
import type { ScreenProps } from "../App";

export function FilesScreen({ ov, refresh, nav }: ScreenProps) {
  const s = ov.session;
  const sid = s.session_id;
  const [over, setOver] = useState(false);
  const [tab, setTab] = useState<"upload" | "s3" | "sample">("upload");
  const [s3, setS3] = useState({ uri: "", access_key: "", secret_key: "", region: "" });
  const [preview, setPreview] = useState<string | null>(null);
  const [results, setResults] = useState<{ file: string; error: string }[]>([]);
  const samples = useLoad(() => get<{ key: string; name: string }[]>("/samples"), []);
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
            <button className={tab === "upload" ? "on" : ""} onClick={() => setTab("upload")}>Upload</button>
            <button className={tab === "s3" ? "on" : ""} onClick={() => setTab("s3")}>From S3 path</button>
            <button className={tab === "sample" ? "on" : ""} onClick={() => setTab("sample")}>Sample files</button>
          </div>
          {tab === "upload" && (
            <label className={`drop ${over ? "over" : ""}`} onDragOver={(e) => { e.preventDefault(); setOver(true); }} onDragLeave={() => setOver(false)}
              onDrop={(e) => { e.preventDefault(); setOver(false); add(Array.from(e.dataTransfer.files)); }}>
              <input type="file" multiple hidden accept=".csv,.txt,.tsv,.xlsx,.xls,.xlsm" onChange={(e) => add(Array.from(e.target.files ?? []))} />
              <b>Drop files here or click to choose</b><div className="small">CSV, TXT, TSV or Excel. Each sheet is read separately; header rows and footers are found automatically.</div>
            </label>
          )}
          {tab === "s3" && (
            <div className="stack" style={{ maxWidth: 640 }}>
              <p className="small muted">A file or a folder in the client's bucket. Credentials are used for this copy only and are not stored.</p>
              <label className="f"><span><b>S3 path</b></span><input placeholder="s3://client-bucket/drops/tesco/" value={s3.uri} onChange={(e) => setS3({ ...s3, uri: e.target.value })} /></label>
              <div className="grid2">
                <label className="f"><span>Access key id</span><input value={s3.access_key} onChange={(e) => setS3({ ...s3, access_key: e.target.value })} /></label>
                <label className="f"><span>Secret access key</span><input type="password" value={s3.secret_key} onChange={(e) => setS3({ ...s3, secret_key: e.target.value })} /></label>
                <label className="f"><span>Region (optional)</span><input placeholder="eu-north-1" value={s3.region} onChange={(e) => setS3({ ...s3, region: e.target.value })} /></label>
              </div>
              <div><button className="primary" disabled={!s3.uri.startsWith("s3://") || a.busy} onClick={() => a.run(async () => {
                await post(`/sessions/${sid}/files/s3`, { uri: s3.uri, access_key: s3.access_key || null, secret_key: s3.secret_key || null, region: s3.region || null });
                setS3({ ...s3, secret_key: "" }); refresh();
              }, "Files copied from S3")}>Copy files</button></div>
            </div>
          )}
          {tab === "sample" && (
            <div className="stack">
              <p className="small muted">Dummy retailer files for demos (seeded with <code>python -m onboarding_poc seed</code>).</p>
              {(samples.data ?? []).map((x) => (
                <div className="row" key={x.key}><span className="mono grow">{x.key}</span>
                  <button className="small" disabled={a.busy} onClick={() => a.run(async () => { await post(`/sessions/${sid}/files/sample`, { key: x.key }); refresh(); }, `${x.name} added`)}>Add</button></div>
              ))}
              {samples.data?.length === 0 && <div className="muted">No sample files in storage.</div>}
            </div>
          )}
          {a.busy && <div className="small"><span className="spin" /> Reading files…</div>}
          <ErrorBanner error={a.error} />
          {results.map((r) => <ErrorBanner key={r.file} error={`${r.file}: ${r.error}`} />)}
        </div>
      </Card>

      <Card title={`Files in this session (${s.files.length})`} flush actions={
        <button className="primary" disabled={!s.files.length || go.busy} onClick={() => go.run(async () => { await post(`/sessions/${sid}/analyse`); refresh(); nav(`/s/${sid}/discovery`); })}>
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
          <div className="scroll"><table className="t"><thead><tr>{sh.columns.map((c: string) => <th key={c}>{c}</th>)}</tr></thead>
            <tbody>{sh.rows.map((r: any[], i: number) => <tr key={i}>{r.map((v, j) => <td key={j}>{v}</td>)}</tr>)}</tbody></table></div>
        </Card>
      ))}
    </>
  );
}
