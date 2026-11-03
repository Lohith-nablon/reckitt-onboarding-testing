import { useState } from "react";
import { downloadUrl, get, post } from "../api";
import { Card, ErrorBanner, GateBar, Kpi, Loading, useAction, useLoad } from "../components/ui";
import type { ScreenProps } from "../App";

export function SignoffScreen({ ov, refresh }: ScreenProps) {
  const s = ov.session;
  const sid = s.session_id;
  const ready = useLoad(() => get<any>(`/sessions/${sid}/signoff`), [sid, s.version]);
  const outs = useLoad(() => s.status === "signed_off" ? get<any[]>(`/sessions/${sid}/outputs`) : Promise.resolve([]), [sid, s.status]);
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
            <p>Everything is reviewed. Signing off writes <b>{ready.data.rows.toLocaleString()}</b> bronze-ready rows for {ready.data.tables.join(", ")} to S3,
              with the Veritas mapping JSON, discovery report, validation and the full decision log.</p>
            <p className="small muted">The approved mappings then go to the mapping library: the next file with the same layout skips AI Discovery and mapping review.
              Loading into Delta stays with Reckitt.</p></div>
            : <div className="stack"><p>Not ready yet:</p><ul style={{ margin: 0 }}>{ready.data.problems.map((p: string) => <li key={p}>{p}</li>)}</ul></div>}
        </Card>)}
      {done && <>
        <div className="kpis">
          <Kpi v={ready.data.rows.toLocaleString()} l="bronze-ready rows" />
          <Kpi v={s.outputs.library?.length ?? 0} l="mappings saved to the library" accent />
          <Kpi v={hours == null ? "–" : hours < 1 ? `${Math.round(hours * 60)} min` : `${hours.toFixed(1)} h`} l="session start to sign-off" accent />
          <Kpi v={ov.journey.decisions.total} l="traced steps" />
        </div>
        <Card title="Output in S3" flush actions={<span className="small muted mono">{s.outputs.base}</span>}>
          {!outs.data ? <Loading /> :
            <table className="t"><thead><tr><th>File</th><th /></tr></thead>
              <tbody>{outs.data.map((o) => <tr key={o.key}><td className="mono small">{o.name}</td>
                <td><a href={downloadUrl(sid, o.key)}>Download</a></td></tr>)}</tbody></table>}
        </Card>
      </>}
    </>
  );
}
