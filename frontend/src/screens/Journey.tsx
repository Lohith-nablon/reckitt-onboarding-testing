import { useState } from "react";
import { get, Overview, TraceEvent } from "../api";
import { By, fmtTime, useLoad } from "../components/ui";

const LABEL: Record<string, string> = { done: "Done", in_progress: "In progress", not_started: "Not started", skipped: "Skipped" };

/** M8 File journey: where each file is, what was skipped, who decided. Shown on every screen. */
export function JourneyPanel({ ov }: { ov: Overview }) {
  const j = ov.journey;
  const [open, setOpen] = useState(false);
  const trace = useLoad(() => open ? get<TraceEvent[]>(`/sessions/${j.session_id}/trace`) : Promise.resolve([]), [open, ov.session.version]);
  return (
    <aside className="journey stack">
      <section className="card">
        <div className="hd"><h3>File journey</h3><span className="small muted">M8</span></div>
        <div className="bd stack">
          {j.files.length === 0 && <div className="muted small">No files yet.</div>}
          {j.files.map((f) => (
            <div key={f.file_id}>
              <div className="strong small" style={{ wordBreak: "break-all" }}>{f.name}</div>
              {f.known_layout && <div style={{ margin: "4px 0" }}><span className="tag solid">Known layout · reused</span></div>}
              {f.steps.map((st) => (
                <div className="jstep" key={st.module + st.label}>
                  <span className={`jdot ${st.status}`} />
                  <div>
                    <div className="small"><b>{st.label}</b> <span className="muted">· {LABEL[st.status]}</span></div>
                    {st.note && <div className="small muted">{st.note}</div>}
                    {st.last_event && <div className="small muted">{fmtTime(st.last_event.at)} · {st.last_event.decided_by.startsWith("ai:") ? "AI" : st.last_event.decided_by}</div>}
                  </div>
                </div>
              ))}
            </div>
          ))}
          <div className="small muted">Gates: {Object.entries(j.gates).map(([k, g]) => `${k} ${g.status.replace("_", " ")}`).join(" · ")}</div>
          <div className="small muted">{j.decisions.total} traced steps · people: {j.decisions.people.join(", ") || "none yet"}</div>
          <button className="small" onClick={() => setOpen(!open)}>{open ? "Hide" : "Show"} decision log</button>
        </div>
      </section>
      {open && (
        <section className="card">
          <div className="hd"><h3>Decision log</h3><a className="small" href={`/api/sessions/${j.session_id}/trace`} target="_blank" rel="noreferrer">JSON</a></div>
          <div className="bd scroll" style={{ maxHeight: 520 }}>
            {(trace.data ?? []).slice().reverse().map((e) => (
              <div key={e.event_id} style={{ borderBottom: "1px solid var(--salt-soft)", padding: "6px 0" }} className="small">
                <div className="row"><span className="tag">{e.module}</span><b>{e.step}</b></div>
                <div className="muted">{fmtTime(e.at)} · <By who={e.decided_by} /> {e.confidence != null && `· ${Math.round(e.confidence * 100)}%`}</div>
                {e.subject && <div className="mono muted" style={{ wordBreak: "break-all" }}>{e.subject}</div>}
                {e.why && <div>{e.why}</div>}
                {(e.before != null || e.after != null) && typeof e.after !== "object" &&
                  <div className="mono">{String(e.before ?? "∅")} → {String(e.after ?? "∅")}</div>}
              </div>
            ))}
          </div>
        </section>
      )}
    </aside>
  );
}
