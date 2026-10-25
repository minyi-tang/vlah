import { X } from "lucide-react";
import type { ReactElement } from "react";

import { event_target, format_seconds, node_by_id } from "./replay.ts";
import type { EntitySelection, Recording, TraceEvent } from "./replay.ts";

export function EventInspector({ recording, event, on_select, on_close }: {
  recording: Recording; event: TraceEvent; on_select: (selection: EntitySelection) => void; on_close: () => void;
}): ReactElement {
  const target = event_target(recording, event);
  return <aside className="trace-inspector" aria-label={`Inspect event ${event.kind}`}>
    <header><span>CANONICAL EVENT · #{event.sequence ?? "UNASSIGNED"}</span><button type="button" aria-label="Close inspector" onClick={on_close}><X size={17} /></button></header>
    <h2>{event.kind.replaceAll("_", " ")}</h2>
    <p className="trace-status">{event.actor} · observed {format_seconds(event.at)}</p>
    <div className="trace-inspector-body">
      {target ? <button type="button" className="trace-inline-link" onClick={() => on_select({ kind: "node", id: target })}>Related artifact: {node_by_id(recording, target).title}</button> : <p className="trace-explanation">This event has no artifact node in this recording. Its complete payload remains available below.</p>}
      <label>RECORDED EVENT ENVELOPE</label>
      <pre className="canonical-event">{event.canonical ? JSON.stringify(event.canonical, null, 2) : event.detail}</pre>
      {!event.canonical ? <p className="trace-explanation">This legacy recording did not preserve the full Event envelope.</p> : null}
    </div>
  </aside>;
}
