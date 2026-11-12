import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Canvas, get, Link, post, put } from "../api";
import { By, Card, Conf, ErrorBanner, GateBar, LinkState, Loading, useAction, useLoad } from "../components/ui";
import type { ScreenProps } from "../App";
import { Conversions } from "./Conversions";
import { MappingMetrics } from "../components/MappingMetrics";

const ACTIONS: [string, string][] = [
  ["DIRECT_COLUMN", "Source column"], ["DIRECT_TEXT", "Constant value"], ["CONCATENATION", "Join columns / text"],
  ["DATE", "Date (convert format)"], ["EXPRESSION", "Expression"], ["", "Leave unmapped"],
];

export function MappingScreen({ ov, refresh, nav }: ScreenProps) {
  const s = ov.session;
  const sid = s.session_id;
  const [mid, setMid] = useState<string | undefined>(ov.mappings[0]?.mapping_id);
  useEffect(() => { if (!mid && ov.mappings[0]) setMid(ov.mappings[0].mapping_id); }, [ov.mappings, mid]);
  const cv = useLoad(() => mid ? get<Canvas>(`/sessions/${sid}/mappings/${mid}/canvas`) : Promise.resolve(null), [mid, s.version]);
  const [tab, setTab] = useState<"canvas" | "conversions" | "preview" | "veritas">("canvas");
  const conv = useLoad(() => get<any[]>(`/sessions/${sid}/conversions`), [sid, s.version]);
  const gate = useAction();
  const prop = useAction();
  if (!ov.mappings.length) return (
    <Card title="No mappings yet"><div className="stack"><p className="muted">Run AI Discovery and mapping from Add files first.</p>
      <ErrorBanner error={prop.error} />
      <div><button className="primary" disabled={prop.busy} onClick={() => prop.run(async () => { await post(`/sessions/${sid}/analyse`); refresh(); })}>Run now</button></div></div></Card>);
  const blocking = ov.mappings.reduce((n, m) => n + m.issues.blocking.length, 0);
  const review = ov.mappings.reduce((n, m) => n + m.issues.review.length, 0);
  return (
    <>
      <GateBar name="A" label="mapping approval" gate={s.gates.A}>
        <span className="small muted">{blocking} blocking · {review} below {Math.round(0.8 * 100)}% confidence</span>
        <button disabled={prop.busy} onClick={() => prop.run(async () => { await post(`/sessions/${sid}/mappings/propose`); refresh(); }, "Mappings re-proposed; your changes were kept")}>Re-propose</button>
        {s.gates.A.status === "approved"
          ? <button className="primary" onClick={() => nav(`/s/${sid}/pipeline`)}>Continue to Pipeline</button>
          : <button className="primary" disabled={gate.busy || blocking > 0} title={blocking ? "Map every mandatory column first" : ""}
            onClick={() => gate.run(async () => { await post(`/sessions/${sid}/gates/A/approve`, {}); refresh(); nav(`/s/${sid}/pipeline`); }, "Gate A approved: mapping JSON and SQL written; pipeline started where configured")}>
            {gate.busy ? <><span className="spin" /> Publishing…</> : "Approve and publish JSON + SQL"}</button>}
      </GateBar>
      <ErrorBanner error={gate.error || prop.error} />
      <MappingMetrics sid={sid} version={s.version} />
      {(conv.data ?? []).some((r) => r.issue && r.status === "proposed") && s.gates.A.status !== "approved" &&
        <div className="banner"><b>Conversions to confirm.</b> {(conv.data ?? []).filter((r) => r.issue && r.status === "proposed").length} conversion rule(s) need a decision before Gate A: see the Conversions tab.</div>}
      {s.gates.A.status === "approved" && ov.mappings.some((m) => m.published) && (
        <Card title="Published for Reckitt’s pipeline (Phase 1 output)" flush actions={<span className="small muted">Mapping JSON stored in Delta format · <a href={`/api/sessions/${sid}/mapping-store`} target="_blank" rel="noreferrer">mapping store</a></span>}>
          <table className="t"><thead><tr><th>File → bronze table</th><th>Generated SQL</th><th>Same as Veritas</th></tr></thead>
            <tbody>{ov.mappings.filter((m) => m.published).map((m) => (
              <tr key={m.mapping_id}><td><b>{m.file_name}</b><div className="mono small">{m.published!.target_fqn}</div></td>
                <td className="small"><a href={`/api/sessions/${sid}/pipeline/sql/${m.mapping_id}`}>Download SQL</a>
                  <div className="mono muted" style={{ wordBreak: "break-all" }}>{m.published!.uris.sql}</div></td>
                <td>{m.veritas_match == null ? <span className="muted small">no Veritas mapping</span> : `${Math.round(m.veritas_match * 100)}%`}</td></tr>))}</tbody></table>
        </Card>)}
      <div className="row">
        {ov.mappings.map((m) => (
          <button key={m.mapping_id} className={m.mapping_id === mid ? "primary" : ""} onClick={() => setMid(m.mapping_id)}>
            {m.file_name}{m.sheet ? ` [${m.sheet}]` : ""} → {m.target_table} <span className="small">({m.mapped}/{m.total}{m.issues.blocking.length ? `, ${m.issues.blocking.length} blocking` : ""})</span>
          </button>))}
      </div>
      {!cv.data ? <Loading /> : (
        <>
          <div className="tabs">
            <button className={tab === "canvas" ? "on" : ""} onClick={() => setTab("canvas")}>Source ⇄ target</button>
            <button className={tab === "conversions" ? "on" : ""} onClick={() => setTab("conversions")}>Conversions</button>
            <button className={tab === "preview" ? "on" : ""} onClick={() => setTab("preview")}>Output preview</button>
            <button className={tab === "veritas" ? "on" : ""} onClick={() => setTab("veritas")}>Veritas JSON</button>
          </div>
          {tab === "canvas" && <MappingCanvas c={cv.data} sid={sid} onChange={() => { cv.reload(); refresh(); }} />}
          {tab === "conversions" && <Conversions sid={sid} rules={(conv.data ?? []).filter((r) => r.mapping_id === cv.data!.mapping_id)} reload={() => { conv.reload(); cv.reload(); refresh(); }} />}
          {tab === "preview" && <Preview sid={sid} mid={cv.data.mapping_id} version={s.version} />}
          {tab === "veritas" && (
            <Card title={`Veritas mapping · ${cv.data.target_table}`} actions={cv.data.veritas ? <a className="btn" href={`/api/sessions/${sid}/mappings/${cv.data.mapping_id}/veritas`}>Download JSON</a> : null}>
              {cv.data.veritas_warnings.length > 0 && <div className="banner" style={{ marginBottom: 10 }}>{cv.data.veritas_warnings.join(" · ")}</div>}
              {cv.data.veritas ? <pre className="json">{JSON.stringify(cv.data.veritas, null, 2)}</pre> : <div className="muted">This target has no Veritas dataset.</div>}
            </Card>)}
        </>
      )}
    </>
  );
}

function Preview({ sid, mid, version }: { sid: string; mid: string; version: number }) {
  const p = useLoad(() => get<any>(`/sessions/${sid}/mappings/${mid}/preview?rows=20`), [mid, version]);
  if (!p.data) return <Loading />;
  return (
    <Card title="First 20 rows after mapping" flush>
      {p.data.error && <div style={{ padding: 12 }}><ErrorBanner error={p.data.error} /></div>}
      <div className="scroll rows"><table className="t"><thead><tr>{p.data.columns.map((c: string) => <th key={c}>{c}</th>)}</tr></thead>
        <tbody>{p.data.rows.map((r: any[], i: number) => <tr key={i}>{r.map((v, j) => <td key={j} className="small">{v ?? <span className="muted">null</span>}</td>)}</tr>)}</tbody></table></div>
    </Card>
  );
}

const ENGINE: Record<string, string> = { llm: "AI", rules: "Rules", profile: "Business fields", veritas_conventions: "Veritas conventions", human: "Person" };

type Line = { x1: number; y1: number; x2: number; y2: number; link: Link };

function MappingCanvas({ c, sid, onChange }: { c: Canvas; sid: string; onChange: () => void }) {
  const box = useRef<HTMLDivElement>(null);
  const srcRefs = useRef<Record<string, HTMLDivElement | null>>({});
  const tgtRefs = useRef<Record<string, HTMLDivElement | null>>({});
  const [lines, setLines] = useState<Line[]>([]);
  const [selT, setSelT] = useState<string | null>(null);
  const [selS, setSelS] = useState<string | null>(null);
  const [filter, setFilter] = useState<"all" | "review" | "unmapped">("all");
  const a = useAction();
  const key = (x: string) => x.toLowerCase().replace(/[^a-z0-9]/g, "");
  const linkBy = useMemo(() => Object.fromEntries(c.links.map((l) => [l.target, l])), [c]);
  const targets = c.targets.filter((t) => {
    const l = linkBy[t.column];
    return filter === "all" || (filter === "review" ? l?.needs_review || l?.blocking : !l?.action);
  });
  const srcKey = useMemo(() => Object.fromEntries(c.sources.map((x) => [key(x.column), x.column])), [c]);

  useLayoutEffect(() => {
    const draw = () => {
      const b = box.current?.getBoundingClientRect();
      if (!b) return;
      const out: Line[] = [];
      for (const t of targets) {
        const l = linkBy[t.column];
        const te = tgtRefs.current[t.column];
        if (!l || !te) continue;
        for (const sname of l.sources) {
          const se = srcRefs.current[srcKey[key(sname)] ?? sname];
          if (!se) continue;
          const sr = se.getBoundingClientRect(), tr = te.getBoundingClientRect();
          out.push({ x1: sr.right - b.left, y1: sr.top + sr.height / 2 - b.top, x2: tr.left - b.left, y2: tr.top + tr.height / 2 - b.top, link: l });
        }
      }
      setLines(out);
    };
    draw();
    const ro = new ResizeObserver(draw);
    if (box.current) ro.observe(box.current);
    return () => ro.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [c, filter]);

  const accept = (targets: string[]) => a.run(async () => { await post(`/sessions/${sid}/mappings/${c.mapping_id}/accept`, { targets }); onChange(); }, `${targets.length} link(s) accepted`);
  const highAi = c.links.filter((l) => l.state === "ai_suggested" && !l.needs_review && l.action).map((l) => l.target);
  const sel = selT ? linkBy[selT] : null;
  const hl = (l: Link) => (selT ? l.target === selT : selS ? l.sources.some((x) => key(x) === key(selS)) : false);

  return (
    <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr) 330px", gap: 16, alignItems: "start" }}>
      <Card title={<>{c.file_name} → {c.target_table} <span className="small muted">v{c.version} · {c.origin}</span></>} flush actions={<>
        <select style={{ width: 160 }} value={filter} onChange={(e) => setFilter(e.target.value as any)}>
          <option value="all">All target columns</option><option value="review">Needs a decision</option><option value="unmapped">Unmapped</option></select>
        <button className="small" disabled={!highAi.length || a.busy} onClick={() => accept(highAi)}>Accept {highAi.length} confident AI links</button>
      </>}>
        <div style={{ padding: "8px 12px" }} className="legend">
          <span><i /> accepted / human modified / library</span><span><i className="ai" /> AI suggested</span><span><i className="rev" /> needs a decision</span>
          <span>Click a target to edit, a source to see where it goes.</span>
        </div>
        <ErrorBanner error={a.error} />
        <div className="canvas" ref={box}>
          <div className="col">
            <h4>Retailer file ({c.sources.length})</h4>
            {c.sources.map((x) => (
              <div key={x.column} ref={(el) => { srcRefs.current[x.column] = el; }} className={`node ${x.used_by.length ? "" : "unused"} ${selS === x.column ? "sel" : ""} ${sel?.sources.some((s) => key(s) === key(x.column)) ? "hl" : ""}`}
                onClick={() => { setSelS(selS === x.column ? null : x.column); setSelT(null); }} title={x.meaning ?? ""}>
                <div className="nm" title={x.column}>{x.column}</div>
                <div className="sub">{x.semantic_type ?? "unknown"} · {x.examples.slice(0, 2).join(", ")}</div>
              </div>))}
          </div>
          <div />
          <div className="col">
            <h4>Target · {c.target_table}</h4>
            {targets.map((t) => {
              const l = linkBy[t.column];
              const cls = `node ${l?.blocking ? "blocking" : l?.needs_review ? "review" : ""} ${selT === t.column ? "sel" : ""} ${selS && l && hl(l) ? "hl" : ""}`;
              return (
                <div key={t.column} ref={(el) => { tgtRefs.current[t.column] = el; }} className={cls} onClick={() => { setSelT(t.column); setSelS(null); }}>
                  <div className="row" style={{ gap: 6 }}>
                    <span className="nm grow" title={t.column}>{t.column}</span>
                    {t.mandatory && <span className="tag flag">req</span>}
                    {l?.action && <Conf v={l.confidence} />}
                  </div>
                  <div className="sub">{l?.action ? (l.sources.length ? l.action.toLowerCase().replace("_", " ") : l.describe) : l?.blocking ? "mandatory: needs a mapping" : `unmapped · ${t.field_source}`}</div>
                </div>);
            })}
          </div>
          <svg width="100%" height="100%">
            {lines.map((ln, i) => {
              const l = ln.link, on = hl(l), dim = (selT || selS) && !on;
              const solid = ["accepted", "human_modified", "library"].includes(l.state ?? "");
              const mx = (ln.x1 + ln.x2) / 2;
              return <path key={i} d={`M ${ln.x1} ${ln.y1} C ${mx} ${ln.y1}, ${mx} ${ln.y2}, ${ln.x2} ${ln.y2}`} fill="none"
                stroke={l.needs_review ? "var(--black)" : "var(--blue)"} strokeWidth={on ? 3 : solid ? 2 : 1.5}
                strokeDasharray={solid ? undefined : l.needs_review ? "3 3" : "6 4"} opacity={dim ? 0.18 : 0.9} />;
            })}
          </svg>
        </div>
      </Card>
      <div className="editor">
        {sel ? <LinkEditor key={`${sel.target}|${sel.describe}|${sel.state}`} link={sel} c={c} sid={sid} onSaved={onChange} onAccept={() => accept([sel.target])} />
          : <Card title="Edit a link"><p className="muted small">Select a target column on the right. You can accept the AI suggestion, change it to any source column,
            a constant, joined columns, a date conversion or an expression, or leave it unmapped. Your change is marked "Human modified" and kept on re-proposal.</p></Card>}
      </div>
    </div>
  );
}

function LinkEditor({ link, c, sid, onSaved, onAccept }: { link: Link; c: Canvas; sid: string; onSaved: () => void; onAccept: () => void }) {
  const [action, setAction] = useState(link.action ?? "");
  const [src, setSrc] = useState(link.sources[0] ?? "");
  const [value, setValue] = useState(link.value ?? "");
  const [parts, setParts] = useState(link.parts.length ? link.parts : [{ column: link.sources[0] ?? null, text: null }]);
  const [dateFmt, setDateFmt] = useState(link.date_format ?? "%Y-%m-%d");
  const [outFmt, setOutFmt] = useState(link.target_date_format ?? "yyyyMMdd");
  const [expr, setExpr] = useState(link.expression ?? "");
  const [note, setNote] = useState("");
  const a = useAction();
  const t = c.targets.find((x) => x.column === link.target)!;
  const cols = c.sources.map((x) => x.column);
  const change = (): Record<string, unknown> => {
    switch (action) {
      case "DIRECT_COLUMN": return { action, source_columns: [src] };
      case "DIRECT_TEXT": return { action, value };
      case "CONCATENATION": return { action, parts, source_columns: parts.filter((p) => p.column).map((p) => p.column) };
      case "DATE": return { action, source_columns: [src], date_format: dateFmt, target_date_format: outFmt || null };
      case "EXPRESSION": return { action, expression: expr };
      default: return { action: null };
    }
  };
  const save = () => a.run(async () => { await put(`/sessions/${sid}/mappings/${c.mapping_id}/links/${encodeURIComponent(link.target)}`, { change: change(), note: note || null }); onSaved(); }, `${link.target} saved`);
  const isLookup = link.action === "LOOKUP";
  return (
    <Card title={<span className="mono">{link.target}</span>}>
      <div className="stack">
        <div className="small muted">{t.dtype}{t.mandatory ? " · mandatory" : ""}{t.pk ? " · key" : ""} · filled from {t.field_source}{t.description && ` · ${t.description}`}</div>
        <div className="row"><LinkState state={link.state} />{link.action && <Conf v={link.confidence} />}{link.decided_by ? <By who={link.decided_by} /> : <span className="tag">{ENGINE[link.engine] ?? link.engine}</span>}</div>
        <div className="banner"><div><b>Current:</b> <span className="mono">{link.describe || "unmapped"}</span><div className="small muted">{link.rationale}</div></div></div>
        {isLookup ? <p className="small muted">Filled by {link.lookup?.name.startsWith("harmonisation") ? "code harmonisation (Harmonisation screen)" : "an MDM lookup"}; nothing to edit here.</p> : <>
          {link.state === "ai_suggested" && link.action && <button onClick={onAccept}>Accept AI suggestion</button>}
          <label className="f"><span><b>How to fill it</b></span>
            <select value={action} onChange={(e) => setAction(e.target.value)}>{ACTIONS.map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></label>
          {(action === "DIRECT_COLUMN" || action === "DATE") && <label className="f"><span>Source column</span>
            <select value={src} onChange={(e) => setSrc(e.target.value)}><option value="">–</option>{cols.map((x) => <option key={x}>{x}</option>)}</select></label>}
          {action === "DIRECT_TEXT" && <label className="f"><span>Value</span><input value={value} onChange={(e) => setValue(e.target.value)} /></label>}
          {action === "DATE" && <div className="grid2" style={{ gridTemplateColumns: "1fr 1fr" }}>
            <label className="f"><span>Source format (Python)</span><input value={dateFmt} onChange={(e) => setDateFmt(e.target.value)} placeholder="%d-%m-%Y" /></label>
            <label className="f"><span>Output format (Java)</span><input value={outFmt} onChange={(e) => setOutFmt(e.target.value)} placeholder="yyyyMMdd" /></label></div>}
          {action === "CONCATENATION" && <div className="stack">
            {parts.map((p, i) => (
              <div className="row" key={i}>
                <select style={{ width: 110 }} value={p.column !== null ? "col" : "text"} onChange={(e) => { const n = [...parts]; n[i] = e.target.value === "col" ? { column: cols[0], text: null } : { column: null, text: "" }; setParts(n); }}>
                  <option value="col">column</option><option value="text">text</option></select>
                {p.column !== null ? <select className="grow" value={p.column} onChange={(e) => { const n = [...parts]; n[i] = { column: e.target.value, text: null }; setParts(n); }}>{cols.map((x) => <option key={x}>{x}</option>)}</select>
                  : <input className="grow" value={p.text ?? ""} onChange={(e) => { const n = [...parts]; n[i] = { column: null, text: e.target.value }; setParts(n); }} />}
                <button className="ghost small" onClick={() => setParts(parts.filter((_, j) => j !== i))}>×</button>
              </div>))}
            <button className="small" onClick={() => setParts([...parts, { column: null, text: "" }])}>Add part</button></div>}
          {action === "EXPRESSION" && <label className="f"><span>Expression, e.g. <code>[units] * [pack_size]</code> or <code>coalesce([a], [b])</code></span>
            <textarea rows={3} value={expr} onChange={(e) => setExpr(e.target.value)} className="mono" /></label>}
          <label className="f"><span>Why (kept in the decision log)</span><input value={note} onChange={(e) => setNote(e.target.value)} placeholder="optional" /></label>
          <ErrorBanner error={a.error} />
          <div><button className="primary" disabled={a.busy} onClick={save}>Save change</button></div>
        </>}
        {link.decided_by && <div className="small muted">Last decided by {link.decided_by}{link.decided_at && ` · ${new Date(link.decided_at).toLocaleString()}`}</div>}
      </div>
    </Card>
  );
}
