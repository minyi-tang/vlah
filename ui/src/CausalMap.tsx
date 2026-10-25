import { Focus, Minus, Plus } from "lucide-react";
import { useMemo } from "react";
import type { ReactElement } from "react";

import { layout_recording, node_by_id, session_at, state_at, visible_graph, worker_node, worker_point } from "./replay.ts";
import type { Recording, Selection } from "./replay.ts";
import { knowledge_path, require_point, WORLD_BOUNDS } from "./spatial.ts";
import type { CameraScope, WorldPoint } from "./spatial.ts";
import { useWorldCamera } from "./useWorldCamera.ts";

const STARS = Array.from({ length: 100 }, (_, index) => ({ x: index * 618.034 % 1000, y: (index * 271.719 + Math.sin(index * 2) * 80 + 700) % 700 }));

export function CausalMap({ recording, time, scope, selection, playing, on_navigate, on_select }: {
  recording: Recording; time: number; scope: CameraScope; selection: Selection; playing: boolean;
  on_navigate: (scope: CameraScope) => void; on_select: (selection: NonNullable<Selection>) => void;
}): ReactElement {
  const layout = useMemo(() => layout_recording(recording), [recording]);
  const { viewport_ref, viewport, camera, relative_zoom, zoom, gestures } = useWorldCamera(layout, scope, on_navigate);
  const graph = useMemo(() => visible_graph(recording, time), [recording, time]);
  const selected_node = selection?.kind === "node" ? selection.id : selection?.kind === "worker" ? worker_node(recording, selection.id, time) : null;
  const related = new Set(graph.edges.filter((edge) => edge.source === selected_node || edge.target === selected_node).flatMap((edge) => [edge.source, edge.target]));
  const recent_edges = graph.edges.filter((edge) => time - edge.at < 3 && edge.relation !== "targets baseline").slice(-10);
  function on_screen(point: WorldPoint): boolean {
    const x = camera.x_pixels + point.x * camera.scale;
    const y = camera.y_pixels + point.y * camera.scale;
    return x >= 0 && x <= viewport.width_pixels && y >= 0 && y <= viewport.height_pixels;
  }
  return <div className={`causal-viewport ${playing ? "" : "is-paused"}`} ref={viewport_ref} role="region" aria-label="Recorded causal universe" tabIndex={0} {...gestures}>
    <div className="causal-nebula" /><svg className="distant-stars" viewBox="0 0 1000 700" preserveAspectRatio="none" aria-hidden="true">{STARS.map((star, index) => <circle key={index} cx={star.x} cy={star.y} r={index % 11 ? 0.55 : 1.1} opacity={0.15 + index % 7 * 0.06} />)}</svg>
    <div className="causal-plane" data-camera-scale={camera.scale} data-camera-x={camera.x_pixels} data-camera-y={camera.y_pixels} style={{ transform: `translate(${camera.x_pixels}px, ${camera.y_pixels}px) scale(${camera.scale})`, opacity: viewport.width_pixels ? 1 : 0 }}>
      <svg className="causal-lines" viewBox={`${WORLD_BOUNDS.x} ${WORLD_BOUNDS.y} ${WORLD_BOUNDS.width} ${WORLD_BOUNDS.height}`} style={{ left: WORLD_BOUNDS.x, top: WORLD_BOUNDS.y, width: WORLD_BOUNDS.width, height: WORLD_BOUNDS.height }} aria-hidden="true">
        {graph.edges.map((edge) => {
          const emphasis = selected_node === edge.source || selected_node === edge.target;
          const failed = /refuted|failed|quarantined|reverted|rollback/.test(state_at(node_by_id(recording, edge.target), time));
          return <path key={`${edge.source}:${edge.target}:${edge.relation}`} className={`${edge.relation === "integrated" || edge.relation === "baseline parent" ? "causal-integration" : ""} ${failed ? "causal-failed" : ""}`}
            style={{ opacity: emphasis ? 0.95 : selected_node ? 0.06 : edge.relation === "produced" ? 0.2 : 0.38 }}
            d={knowledge_path(require_point(layout.nodes, edge.source), require_point(layout.nodes, edge.target))} />;
        })}
      </svg>
      {recent_edges.map((edge) => <span aria-hidden="true" className={`causal-meteor ${edge.relation === "integrated" ? "is-integrated" : ""}`} key={`${edge.source}:${edge.target}:${edge.relation}`} style={{ offsetPath: `path("${knowledge_path(require_point(layout.nodes, edge.source), require_point(layout.nodes, edge.target))}")`, offsetDistance: `${Math.min(1, (time - edge.at) / 2) * 100}%`, opacity: Math.max(0, 1 - (time - edge.at) / 3) }}><i style={{ transform: `scale(${1 / camera.scale})` }} /></span>)}
      {graph.nodes.map((node) => {
        const point = require_point(layout.nodes, node.id);
        const state = state_at(node, time);
        const is_selected = selected_node === node.id;
        return <div className="causal-anchor" key={node.id} data-node-id={node.id} data-node-kind={node.kind} data-world-x={point.x} data-world-y={point.y} style={{ left: point.x, top: point.y, transform: `scale(${1 / camera.scale})`, opacity: selected_node && !related.has(node.id) && !is_selected ? 0.22 : 1 }}>
          <button type="button" className={`causal-node kind-${node.kind} ${node.title === "dead end" && state !== "reinstated" ? "is-dead-end" : ""} ${/refuted|failed|quarantined|reverted|rollback/.test(state) ? "is-failed" : ""} ${is_selected ? "is-selected" : ""}`} tabIndex={on_screen(point) ? 0 : -1} aria-label={`Inspect ${node.title}: ${node.id}`} onClick={() => on_select({ kind: "node", id: node.id })}>
            <span className="causal-node-core" /><span className="causal-node-label" style={{ opacity: is_selected || (relative_zoom > 3 && related.has(node.id)) ? 1 : 0 }}>{node.title}<small>{state}</small></span>
          </button>
        </div>;
      })}
      {recording.workers.map((worker) => {
        const current = session_at(recording, worker, time);
        const point = worker_point(recording, layout, worker, time);
        return <div key={worker} className="causal-anchor causal-worker-anchor" data-worker-id={worker} style={{ left: point.x, top: point.y, transform: `scale(${1 / camera.scale})` }}>
          <button type="button" className={`causal-worker ${current ? "is-running" : ""} ${selection?.kind === "worker" && selection.id === worker ? "is-selected" : ""}`} aria-label={`Inspect worker ${worker}`} tabIndex={on_screen(point) ? 0 : -1} onClick={() => on_select({ kind: "worker", id: worker })}><i /><span>{worker}{current ? <small>{current.kind.replaceAll("_", " ")}</small> : null}</span></button>
        </div>;
      })}
    </div>
    <div className="causal-caption"><span>THE WORK GROWS. WORKER IDENTITIES STAY.</span><p>{graph.nodes.length} observed artifacts · {graph.edges.length} recorded causal links</p></div>
    <div className="causal-legend"><span><i className="legend-worker" />Worker</span><span><i className="legend-candidate" />Candidate</span><span><i className="legend-baseline" />Baseline</span><span>Faint trails remain after failure.</span></div>
    <div className="world-camera-controls"><button type="button" aria-label="Zoom out" onClick={() => zoom(1 / 1.6)}><Minus size={15} /></button><span>{relative_zoom.toFixed(1)}x</span><button type="button" aria-label="Zoom in" onClick={() => zoom(1.6)}><Plus size={15} /></button><button type="button" aria-label="Show whole universe" onClick={() => on_navigate({ level: "overview" })}><Focus size={15} /></button></div>
    <p className="causal-navigation-hint">Scroll / pinch to approach · drag to explore · Home to return</p>
  </div>;
}
