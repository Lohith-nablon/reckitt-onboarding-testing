import { useState } from "react";
import { downloadUrl, get, post } from "../api";
import { Card, ErrorBanner, GateBar, Kpi, Loading, useAction, useLoad } from "../components/ui";
import type { ScreenProps } from "../App";

export function SignoffScreen({ ov, refresh }: ScreenProps) {
  const s = ov.session;
  const sid = s.session_id;
  const ready = useLoad(() => get<any>(`/sessions/${sid}/signoff`), [sid, s.version]);
  const outs = useLoad(() => s.status === "signed_off" ? get<any[]>(`/sessions/${sid}/outputs`) : Promise.resolve([]), [sid, s.status]);
  const silver = useLoad(() => s.status === "signed_off" ? get<any[]>(`/sessions/${sid}/silver?rows=10`) : Promise.resolve([]), [sid, s.status]);
  const [note, setNote] = useState("");
  const a = useAction();
  if (!ready.data) return <Loading />;
  const done = s.status === "signed_off";
  const hours = done && s.gates.C.decided_at ? (new Date(s.gates.C.decided_at).getTime() - new Date(s.created_at).getTime()) / 36e5 : null;
  return (
    <>
      <GateBar name="C" label="sign-off" gate={s.gates.C}>
        {!done && <>
          <input style={{ width: 260 }} placeholder="Note (optional)" value={note} onChange={(e) => setNote(e.target.value)} />
          <button className="primary" disabled={!ready.data.ready || a.busy} onClick={() => a.run(async () => { await post(`/sessions/${sid}/gates/C/approve`, { note: note || null }); refresh(); }, "Signed off. Output written and mapping saved to the library")}>
            {a.busy ? <><span className="spin" /> Writing output…</> : "Sign off and write output"}</button></>}
      </GateBar>
      <ErrorBanner error={a.error} />
      {!done && (
        <Card title="Before you sign off">
          {ready.data.ready ? <div className="stack">
            <p>Everything is reviewed. Signing off writes <b>{ready.data.rows.toLocaleString()}</b> harmonised rows to silver in Unity Catalog
              ({ready.data.targets.join(", ")}) and this session’s code decisions to <span className="mono">{ready.data.crosswalk_table}</span>,
              plus the discovery report, validation and the full decision log to ADLS.</p>
            <p className="small muted">The approved mappings then go to the mapping library: the next file with the same layout skips AI Discovery and mapping review.</p></div>
            : <div className="stack"><p>Not ready yet:</p><ul style={{ margin: 0 }}>{ready.data.problems.map((p: string) => <li key={p}>{p}</li>)}</ul></div>}
        </Card>)}
      {done && <>
        <div className="kpis">
          <Kpi v={ready.data.rows.toLocaleString()} l="harmonised rows in silver" />
          <Kpi v={s.outputs.library?.length ?? 0} l="mappings saved to the library" accent />
          <Kpi v={hours == null ? "–" : hours < 1 ? `${Math.round(hours * 60)} min` : `${hours.toFixed(1)} h`} l="session start to sign-off" accent />
          <Kpi v={ov.journey.decisions.total} l="traced steps" />
        </div>
        <Card title="Written to Unity Catalog (silver)" flush>
          <table className="t"><thead><tr><th>Table</th><th>Rows</th></tr></thead>
            <tbody>{(s.outputs.items ?? []).filter((o) => o.fqn).map((o) => <tr key={o.fqn}><td className="mono">{o.fqn}</td><td>{o.rows?.toLocaleString()}</td></tr>)}</tbody></table>
        </Card>
        {(silver.data ?? []).filter((t) => t.kind === "silver").slice(0, 1).map((t) => (
          <Card key={t.fqn} title={`Sample from ${t.fqn}`} flush>
            <div className="scroll rows" style={{ maxHeight: 300 }}><table className="t"><thead><tr>{t.columns.map((c: string) => <th key={c}>{c}</th>)}</tr></thead>
              <tbody>{t.rows.map((r: any[], i: number) => <tr key={i}>{r.map((v, j) => <td key={j} className="small">{v ?? <span className="muted">null</span>}</td>)}</tr>)}</tbody></table></div>
          </Card>))}
        <Card title="Files in ADLS (mapping JSON, SQL, reports, trace)" flush actions={<span className="small muted mono">{s.outputs.base}</span>}>
          {!outs.data ? <Loading /> :
            <table className="t"><thead><tr><th>File</th><th /></tr></thead>
              <tbody>{outs.data.map((o) => <tr key={o.key}><td className="mono small">{o.name}</td>
                <td><a href={downloadUrl(sid, o.key)}>Download</a></td></tr>)}</tbody></table>}
        </Card>
      </>}
    </>
  );
}
