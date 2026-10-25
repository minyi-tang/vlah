import { Activity, ArrowUpRight, CircleHelp, FolderOpen, Orbit, Pause, Play, Radio, RotateCcw, Search, Users, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { ReactElement } from "react";

import { CausalMap } from "./CausalMap.tsx";
import { EventInspector } from "./EventInspector.tsx";
import { RECORDING } from "./recording.ts";
import { event_by_id, event_id, event_target, format_seconds, layout_recording, node_by_id, session_at, validate_recording, worker_node } from "./replay.ts";
import type { Recording, Selection } from "./replay.ts";
import type { CameraScope } from "./spatial.ts";
import { TraceInspector } from "./TraceInspector.tsx";

validate_recording(RECORDING);
const DEFAULT_RECORDING: Recording = RECORDING;

export default function App(): ReactElement {
  const [recording, set_recording] = useState<Recording>(DEFAULT_RECORDING);
  const [time, set_time] = useState(0);
  const [playing, set_playing] = useState(() => !matchMedia("(prefers-reduced-motion: reduce)").matches);
  const [speed, set_speed] = useState(1);
  const [scope, set_scope] = useState<CameraScope>({ level: "overview" });
  const [selection, set_selection] = useState<Selection>(null);
  const [panel, set_panel] = useState<"workers" | "events" | "source" | "run" | null>(null);
  const [query, set_query] = useState("");
  const [following, set_following] = useState(false);
  const [error, set_error] = useState<string | null>(null);
  const [source_label, set_source_label] = useState("CORE RECORDING · SCRIPTED I/O");
  const file_input = useRef<HTMLInputElement>(null);
  const finished = time >= recording.duration_seconds;
  const running = playing && !finished && !following;
  const active_sessions = recording.sessions.filter((session) => session.start <= time && (session.end === null || session.end > time));
  const events = recording.events.filter((event) => event.at <= time);
  const baselines = recording.nodes.filter((node) => node.kind === "baseline" && node.at <= time);
  const current_baseline = baselines.toSorted((left, right) => Number(left.id.slice(9)) - Number(right.id.slice(9))).at(-1);
  const results = events.filter((event) => event.kind === "result_ready");
  const run_state = recording.run_states?.findLast((state) => state.at <= time);
  const report_visible = recording.report && (recording.report_at === undefined || recording.report_at === null || recording.report_at <= time);

  useEffect(() => {
    if (!running) {
      return;
    } else {
      let frame = 0;
      let previous: number | null = null;
      const advance = (milliseconds: number): void => {
        if (previous !== null && !document.hidden) {
          const delta = (milliseconds - previous) / 1000 * speed;
          set_time((current) => Math.min(recording.duration_seconds, current + delta));
        } else {
          previous = null;
        }
        previous = document.hidden ? null : milliseconds;
        frame = requestAnimationFrame(advance);
      };
      const visibility = (): void => { previous = null; };
      document.addEventListener("visibilitychange", visibility);
      frame = requestAnimationFrame(advance);
      return () => { cancelAnimationFrame(frame); document.removeEventListener("visibilitychange", visibility); };
    }
  }, [running, speed, recording.duration_seconds]);

  useEffect(() => {
    if (!following) {
      return;
    } else {
      const controller = new AbortController();
      let last_content = "";
      let in_flight = false;
      const poll = async (): Promise<void> => {
        if (in_flight) {
          return;
        } else {
          in_flight = true;
        }
        try {
          const response = await fetch("/recording.json", { cache: "no-store", signal: controller.signal });
          if (!response.ok) throw new Error(`Recording endpoint returned HTTP ${response.status}.`);
          else {
            const content = await response.text();
            const next: unknown = JSON.parse(content);
            validate_recording(next);
            layout_recording(next);
            if (content !== last_content) {
              set_recording(next);
              set_time(next.duration_seconds);
              set_selection((previous) => previous && (previous.kind === "worker" ? next.workers.includes(previous.id) : previous.kind === "node" ? next.nodes.some((node) => node.id === previous.id) : next.events.some((event) => event_id(event) === previous.id)) ? previous : null);
              set_scope((previous) => previous.level !== "node" || next.nodes.some((node) => node.id === previous.id) ? previous : { level: "overview" });
              last_content = content;
            } else {
              // Keep the camera where the reader left it when the file is unchanged.
            }
            set_error(null);
            set_source_label("OBSERVED RECORDING FILE");
          }
        } catch (failure) {
          if (!controller.signal.aborted) set_error(`Recording disconnected: ${failure instanceof Error ? failure.message : String(failure)} The last valid recording is retained.`);
          else { return; }
        } finally {
          in_flight = false;
        }
      };
      void poll();
      const interval = window.setInterval(() => { void poll(); }, 2000);
      return () => { controller.abort(); window.clearInterval(interval); };
    }
  }, [following]);

  function select(next: NonNullable<Selection>): void {
    set_selection(next);
    set_panel(null);
    set_query("");
    const node_id = next.kind === "node" ? next.id : next.kind === "worker" ? worker_node(recording, next.id, time) : event_target(recording, event_by_id(recording, next.id));
    if (node_id) set_scope({ level: "node", id: node_id });
    else if (next.kind !== "event") set_scope({ level: "overview" });
    else { return; }
  }
  function navigate(next: CameraScope): void {
    set_scope(next);
    set_selection(null);
  }
  function seek(next: number): void {
    set_following(false);
    set_playing(false);
    set_time(next);
    set_selection(null);
    if (scope.level === "node" && node_by_id(recording, scope.id).at > next) set_scope({ level: "overview" });
    else { return; }
  }
  function restart(): void {
    set_following(false); set_time(0); set_playing(true); set_selection(null); set_scope({ level: "overview" });
  }
  async function open_file(file: File): Promise<void> {
    set_following(false);
    try {
      const parsed: unknown = JSON.parse(await file.text());
      validate_recording(parsed);
      layout_recording(parsed);
      set_following(false); set_recording(parsed); set_time(0); set_playing(false);
      set_source_label("IMPORTED RECORDING");
      set_selection(null); set_scope({ level: "overview" }); set_error(null);
    } catch (failure) {
      set_error(`Could not open ${file.name}: ${failure instanceof Error ? failure.message : String(failure)}`);
    }
  }

  const shown_workers = recording.workers.filter((worker) => {
    const session = session_at(recording, worker, time);
    return `${worker} ${session?.goal ?? ""}`.toLowerCase().includes(query.trim().toLowerCase());
  });
  return <main className="replay-app">
    <a className="skip-link" href="#causal-view">Skip to the recorded universe</a>
    <header className="replay-header"><div className="replay-brand"><Orbit size={26} /><strong>vlah.</strong><span>THE OBSERVATORY</span></div>
      <nav aria-label="Recording tools"><button type="button" onClick={() => { set_panel(panel === "workers" ? null : "workers"); set_selection(null); }}><Users size={14} /> Workers</button><button type="button" onClick={() => { set_panel(panel === "events" ? null : "events"); set_selection(null); }}><Activity size={14} /> Event trail</button><button type="button" onClick={() => { set_panel(panel === "run" ? null : "run"); set_selection(null); }}>Run report</button><button type="button" onClick={() => { set_panel(panel === "source" ? null : "source"); set_selection(null); }}><CircleHelp size={14} /> Provenance</button></nav>
      <label className="replay-search"><Search size={13} /><input aria-label="Find a worker" placeholder="Find a worker or current goal" value={query} onChange={(event) => { set_query(event.target.value); set_panel("workers"); set_selection(null); }} /></label>
    </header>
    <div className="replay-heading"><div><h1>{recording.name}</h1><p>Not a tree of new agents. A growing history of work.</p></div><span className="replay-source">{following ? error ? "RECORDING DISCONNECTED" : "FOLLOWING RECORDING FILE" : source_label}</span></div>
    <div className="replay-metrics" aria-label="Observed runtime state"><span><strong>{recording.workers.length - 1}</strong> fixed workers + compose</span><span><strong>{active_sessions.length}</strong> observed running sessions</span><span><strong>{results.length}</strong> worker results</span><span><strong>{current_baseline?.title ?? "—"}</strong> shared baseline</span></div>
    <nav className="approach-navigation" aria-label="Exploration scope"><button type="button" onClick={() => navigate({ level: "overview" })}>Whole run</button>{[...new Set(recording.nodes.filter((node) => node.at <= time && node.approach !== "shared").map((node) => node.approach))].map((approach) => <button type="button" key={approach} onClick={() => navigate({ level: "approach", id: approach })}>{approach}</button>)}{scope.level === "node" ? <span>{node_by_id(recording, scope.id).title}</span> : null}</nav>
    {error ? <div className="replay-error" role="alert">{error}<button type="button" aria-label="Dismiss error" onClick={() => set_error(null)}><X size={15} /></button></div> : null}
    <section id="causal-view" className="replay-stage">
      <CausalMap recording={recording} time={time} scope={scope} selection={selection} playing={running} on_navigate={navigate} on_select={select} />
      {selection ? selection.kind === "event" ? <EventInspector recording={recording} event={event_by_id(recording, selection.id)} on_select={select} on_close={() => set_selection(null)} /> : <TraceInspector key={`${selection.kind}:${selection.id}`} recording={recording} selection={selection} time={time} on_select={select} on_close={() => set_selection(null)} /> : null}
      {panel ? <aside className={`trace-drawer drawer-${panel}`}><header><h2>{panel === "workers" ? `All ${recording.workers.length - 1} workers + compose` : panel === "events" ? "Recorded event trail" : panel === "run" ? "Run snapshots & report" : "Where this view comes from"}</h2><button type="button" aria-label="Close panel" onClick={() => set_panel(null)}><X size={17} /></button></header>
        {panel === "workers" ? <div className="trace-drawer-content">{shown_workers.length === 0 ? <p>No matching worker or current goal.</p> : shown_workers.map((worker) => {
          const session = session_at(recording, worker, time);
          const observed = recording.sessions.some((entry) => entry.actor === worker && entry.start <= time);
          return <button type="button" className="trace-worker-row" key={worker} onClick={() => select({ kind: "worker", id: worker })}><span><strong>{worker}</strong><small>{session?.goal ?? (observed ? "No running session at this recorded moment." : "Session state has not been observed.")}</small></span><em>{session ? "running" : observed ? "idle" : "unknown"}</em></button>;
        })}</div> : panel === "events" ? <div className="trace-drawer-content">{events.toReversed().map((event) =>
          <button type="button" className="trace-event-row" key={event_id(event)} onClick={() => select({ kind: "event", id: event_id(event) })}><time>{format_seconds(event.at)}</time><span>{event.kind.replaceAll("_", " ")}<small>{event.actor} · merge #{event.sequence ?? "unassigned"}</small></span><ArrowUpRight size={13} /></button>
        )}</div> : panel === "run" ? <div className="trace-drawer-content trace-provenance">
          <h3>Recorded RunStatus</h3>{run_state ? <><p>Observed at {format_seconds(run_state.at)}. This is a timestamped snapshot, not an inferred current state.</p><pre className="canonical-event">{JSON.stringify(run_state.status, null, 2)}</pre></> : <p>No RunStatus snapshot was captured at this point.</p>}
          <h3>Captured report</h3>{report_visible ? <pre className="canonical-event">{JSON.stringify(recording.report, null, 2)}</pre> : <p>No report was captured at this replay position.</p>}
          {recording.report_error ? <p role="alert">Report unavailable: {recording.report_error}</p> : null}
        </div> : <div className="trace-drawer-content trace-provenance">
          <p>{recording.provenance}</p>
          <p>Worker identities do not multiply. Task/session observations, claim references, candidate parents and accepted baselines form the visible graph. Positions and light trails illustrate those records; they are not measured physical motion.</p>
          <p>Checkpoint/context fork is not supported by the current harness, and this view does not invent it. Session completion is not a judge verdict.</p>
          <button type="button" onClick={() => file_input.current?.click()}><FolderOpen size={15} /> Open recording JSON</button>
          <button type="button" aria-pressed={following} onClick={() => { set_playing(false); set_following(!following); }}><Radio size={15} /> {following ? "Stop following recording.json" : "Follow recording.json"}</button>
          <p>File following observes a recorder's published JSON. It does not connect to or launch a production worker. Errors are shown without substituting demo data.</p>
          <code>uv run python -m ui.record_replay</code>
        </div>}
      </aside> : null}
    </section>
    <footer className="replay-controls"><button type="button" aria-label={running ? "Pause replay" : finished ? "Replay from beginning" : "Play replay"} onClick={() => { set_following(false); if (finished) restart(); else set_playing(!playing); }}>{running ? <Pause size={15} /> : <Play size={15} />}</button><button type="button" aria-label="Restart replay" onClick={restart}><RotateCcw size={14} /></button><time>{format_seconds(time)}</time><input type="range" aria-label="Replay position" min={0} max={recording.duration_seconds} step={0.01} value={time} onChange={(event) => seek(Number(event.target.value))} /><time>{format_seconds(recording.duration_seconds)}</time><label><span className="sr-only">Playback speed</span><select aria-label="Playback speed" value={speed} onChange={(event) => set_speed(Number(event.target.value))}><option value={1}>1×</option><option value={2}>2×</option><option value={4}>4×</option></select></label><span>{following ? "FILE FOLLOW" : finished ? "END OF RECORDING" : running ? "REPLAYING" : "PAUSED"}</span></footer>
    <input ref={file_input} type="file" accept=".json,application/json" className="sr-only" tabIndex={-1} aria-label="Open a recording file" onChange={(event) => { const file = event.target.files?.[0]; if (file) void open_file(file); else { return; } event.target.value = ""; }} />
  </main>;
}
