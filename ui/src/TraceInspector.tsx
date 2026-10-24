import { ArrowDownRight, ArrowUpLeft, X } from "lucide-react";
import { useState } from "react";
import type { ReactElement } from "react";

import { event_target, format_seconds, node_by_id, session_at, state_at } from "./replay.ts";
import type { EntitySelection, Recording } from "./replay.ts";

export function TraceInspector({ recording, selection, time, on_select, on_close }: {
  recording: Recording; selection: EntitySelection; time: number;
  on_select: (selection: EntitySelection) => void; on_close: () => void;
}): ReactElement {
  const [tab, set_tab] = useState<"overview" | "history" | "links">("overview");
  const worker = selection.kind === "worker" ? selection.id : null;
  const node = selection.kind === "node" ? node_by_id(recording, selection.id) : null;
  const active = worker ? session_at(recording, worker, time) : undefined;
  const sessions = recording.sessions.filter((session) => session.actor === worker && session.start <= time);
  const contributions = recording.nodes.filter((entry) => entry.actor === worker && entry.at <= time && entry.kind !== "task");
  const links = node ? recording.edges.filter((edge) => (edge.target === node.id || edge.source === node.id) && edge.at <= time) : [];
  const events = node ? recording.events.filter((event) => event.at <= time && event_target(recording, event) === node.id) : [];
  const current = active ?? sessions.at(-1);
  const label = node?.title ?? worker!;
  const tabs = ["overview", "history", "links"] as const;
  return <aside className="trace-inspector" aria-label={`Inspect ${label}`}>
    <header><span>{node?.kind.toUpperCase() ?? "WORKER · FIXED IDENTITY"}</span><button type="button" aria-label="Close inspector" onClick={on_close}><X size={17} /></button></header>
    <h2>{label}</h2><p className="trace-status">{node ? state_at(node, time) : active ? `Running ${active.kind.replaceAll("_", " ")}` : sessions.length ? "Idle at this point in the recording" : "No session observed yet"}</p>
    <div role="tablist" className="trace-tabs" aria-label="Inspector sections" onKeyDown={(event) => {
      const index = tabs.indexOf(tab);
      const next = event.key === "ArrowRight" ? (index + 1) % 3 : event.key === "ArrowLeft" ? (index + 2) % 3 : event.key === "Home" ? 0 : event.key === "End" ? 2 : -1;
      if (next >= 0) { event.preventDefault(); set_tab(tabs[next]); event.currentTarget.querySelectorAll<HTMLButtonElement>("button")[next].focus(); } else { return; }
    }}>{tabs.map((name) => <button type="button" role="tab" id={`trace-${name}`} aria-controls={`trace-panel-${name}`} aria-selected={tab === name} tabIndex={tab === name ? 0 : -1} key={name} onClick={() => set_tab(name)}>{name}</button>)}</div>
    {tabs.map((name) => <div key={name} role="tabpanel" hidden={tab !== name} id={`trace-panel-${name}`} aria-labelledby={`trace-${name}`} className="trace-inspector-body">
      {name === "overview" ? <>
        <label>{node ? "RECORDED CONTENT" : active ? "CURRENT GOAL" : "LAST OBSERVED GOAL"}</label>
        <p className="trace-goal">{node?.detail ?? current?.goal ?? "This worker has no observed task at this time."}</p>
        {node ? <><label>PROVENANCE</label><code>{node.source}</code><p>First observed at {format_seconds(node.at)}.</p><button type="button" className="trace-inline-link" onClick={() => on_select({ kind: "worker", id: node.actor })} disabled={!recording.workers.includes(node.actor)}>Author: {node.actor}</button></> : <>
          <div className="trace-totals"><span><b>{sessions.length}</b> observed sessions</span><span><b>{contributions.length}</b> published artifacts</span></div>
          <p className="trace-explanation">A new session or follow-up task does not create another worker. These are this worker's observations in the recorded run, not current production liveness.</p>
        </>}
      </> : name === "history" ? <>
        {node ? node.states.filter((state) => state.at <= time).map((state, index) => <p className="trace-history" key={index}><time>{format_seconds(state.at)}</time><span>{state.status}<small>{state.sequence === null ? "artifact / session observation" : `merge sequence ${state.sequence}`}</small></span></p>)
          : sessions.toReversed().map((session) => <button className="trace-history" type="button" key={session.id} onClick={() => on_select({ kind: "node", id: session.task })}><time>{format_seconds(session.start)}</time><span>{session.kind.replaceAll("_", " ")}<small>{session.end !== null && session.end <= time ? `Session ended ${format_seconds(session.end)}` : "Session running"}</small></span></button>)}
        {events.map((event, index) => <details className="trace-payload" key={index}><summary>{event.kind.replaceAll("_", " ")} · #{event.sequence}</summary><pre>{event.canonical ? JSON.stringify(event.canonical, null, 2) : event.detail}</pre></details>)}
      </> : <>
        <p className="trace-explanation">Every line below names its recorded source field. Temporal proximity alone never creates an inheritance edge.</p>
        {node ? links.map((edge) => {
          const incoming = edge.target === node.id;
          const related = node_by_id(recording, incoming ? edge.source : edge.target);
          return <button type="button" className="trace-link" key={`${edge.source}:${edge.target}:${edge.relation}`} onClick={() => on_select({ kind: "node", id: related.id })}>{incoming ? <ArrowUpLeft size={14} /> : <ArrowDownRight size={14} />}<span>{related.title}<small>{edge.relation} · {edge.evidence}</small></span></button>;
        }) : contributions.map((entry) => <button type="button" className="trace-link" key={entry.id} onClick={() => on_select({ kind: "node", id: entry.id })}><ArrowDownRight size={14} /><span>{entry.title}<small>{entry.source}</small></span></button>)}
      </>}
    </div>)}
  </aside>;
}
