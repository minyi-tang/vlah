import type { WorldLayout, WorldPoint } from "./spatial.ts";
import { point_on_path, require_point } from "./spatial.ts";

export interface TraceState { at: number; status: string; sequence: number | null }
export interface TraceNode {
  id: string; kind: "task" | "claim" | "candidate" | "baseline";
  title: string; detail: string; actor: string; approach: string;
  at: number; order: number; source: string; states: readonly TraceState[];
}
export interface TraceEdge { source: string; target: string; relation: string; at: number; evidence: string }
export type JsonValue = string | number | boolean | null | readonly JsonValue[] | { readonly [key: string]: JsonValue };
export interface JsonObject { readonly [key: string]: JsonValue }
export interface TraceEvent { at: number; sequence: number | null; kind: string; actor: string; target: string; detail: string; canonical?: JsonObject | null }
export interface TraceSession { id: string; actor: string; task: string; kind: string; goal: string; start: number; end: number | null }
export interface Recording {
  version: number; name: string; provenance: string; duration_seconds: number;
  workers: readonly string[]; compose_actor: string;
  nodes: readonly TraceNode[]; edges: readonly TraceEdge[];
  events: readonly TraceEvent[]; sessions: readonly TraceSession[];
  run_states?: readonly { at: number; status: JsonObject }[];
  report?: JsonObject | null; report_at?: number | null; report_error?: string | null;
}
export type Selection = { kind: "worker"; id: string } | { kind: "node"; id: string } | { kind: "event"; id: string } | null;
export type EntitySelection = Exclude<NonNullable<Selection>, { kind: "event" }>;

export function event_id(event: TraceEvent): string {
  return typeof event.canonical?.local_id === "string" ? event.canonical.local_id : `event:${event.sequence ?? event.at}:${event.actor}:${event.kind}:${event.target}`;
}

export function event_by_id(recording: Recording, identity: string): TraceEvent {
  const event = recording.events.find((candidate) => event_id(candidate) === identity);
  if (!event) throw new Error(`No recorded event ${identity}.`);
  else return event;
}

export function node_by_id(recording: Recording, identity: string): TraceNode {
  const node = recording.nodes.find((candidate) => candidate.id === identity);
  if (!node) {
    throw new Error(`The recording has no node ${identity}.`);
  } else {
    return node;
  }
}

export function state_at(node: TraceNode, at_seconds: number): string {
  return node.states.findLast((state) => state.at <= at_seconds)?.status ?? "not yet observed";
}

export function session_at(recording: Recording, worker: string, at_seconds: number): TraceSession | undefined {
  return recording.sessions.findLast((session) => session.actor === worker && session.start <= at_seconds && (session.end === null || session.end > at_seconds));
}

export function worker_node(recording: Recording, worker: string, at_seconds: number): string | null {
  const session = session_at(recording, worker, at_seconds);
  return session?.task ?? recording.nodes.findLast((node) => node.actor === worker && node.at <= at_seconds)?.id ?? null;
}

export function worker_point(recording: Recording, layout: WorldLayout, worker: string, at_seconds: number): WorldPoint {
  const history = recording.nodes.filter((node) => node.actor === worker && node.at <= at_seconds);
  const latest = history.at(-1);
  if (!latest) {
    const index = recording.workers.indexOf(worker);
    return { x: -550 + index % 20 * 24, y: 550 + Math.floor(index / 20) * 18 };
  } else {
    const target = require_point(layout.nodes, latest.id);
    const previous = history.findLast((node) => node.at < latest.at);
    const progress = Math.min(1, Math.max(0, at_seconds - latest.at));
    return previous ? point_on_path(require_point(layout.nodes, previous.id), target, 1 - (1 - progress) ** 3) : target;
  }
}

export function visible_graph(recording: Recording, at_seconds: number): { nodes: readonly TraceNode[]; edges: readonly TraceEdge[] } {
  const nodes = recording.nodes.filter((node) => node.at <= at_seconds);
  const identities = new Set(nodes.map((node) => node.id));
  return { nodes, edges: recording.edges.filter((edge) => edge.at <= at_seconds && identities.has(edge.source) && identities.has(edge.target)) };
}

export function event_target(recording: Recording, event: TraceEvent): string | null {
  return recording.nodes.find((node) => node.id === event.target || node.id === `candidate:${event.target}`)?.id ?? null;
}

export function format_seconds(seconds: number): string {
  return `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(Math.floor(seconds % 60)).padStart(2, "0")}`;
}

function is_object(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function is_json(value: unknown): value is JsonValue {
  return value === null || typeof value === "string" || typeof value === "boolean"
    || (typeof value === "number" && Number.isFinite(value))
    || (Array.isArray(value) && value.every(is_json))
    || (is_object(value) && Object.values(value).every(is_json));
}

export function validate_recording(value: unknown): asserts value is Recording {
  if (!is_object(value) || value.version !== 1 || typeof value.name !== "string" || typeof value.provenance !== "string"
      || typeof value.compose_actor !== "string" || typeof value.duration_seconds !== "number" || !Number.isFinite(value.duration_seconds) || value.duration_seconds <= 0
      || !Array.isArray(value.workers) || !value.workers.every((worker: unknown) => typeof worker === "string")
      || new Set(value.workers).size !== value.workers.length || !value.workers.includes(value.compose_actor)
      || !Array.isArray(value.nodes) || value.nodes.length === 0 || !Array.isArray(value.edges) || !Array.isArray(value.events) || !Array.isArray(value.sessions)) {
    throw new Error("Expected a VLAH recording with version 1, workers, nodes, edges, events and sessions.");
  } else {
    const duration_seconds = value.duration_seconds;
    const finite_time = (time: unknown): time is number => typeof time === "number" && Number.isFinite(time) && time >= 0 && time <= duration_seconds;
    const sequence = (number: unknown): boolean => number === null || (typeof number === "number" && Number.isInteger(number) && number >= 0);
    const fields = (record: Record<string, unknown>, names: readonly string[]): boolean => names.every((name) => typeof record[name] === "string");
    const ids = new Set<string>();
    for (const node of value.nodes) {
      if (!is_object(node) || !fields(node, ["id", "kind", "title", "detail", "actor", "approach", "source"])
          || !["task", "claim", "candidate", "baseline"].includes(String(node.kind))
          || (node.kind === "baseline" && !/^baseline@\d+$/.test(String(node.id)))
          || !finite_time(node.at) || typeof node.order !== "number" || !Number.isInteger(node.order)
          || !Array.isArray(node.states) || !node.states.every((state: unknown) => is_object(state) && finite_time(state.at) && typeof state.status === "string" && sequence(state.sequence))
          || ids.has(String(node.id))) {
        throw new Error("A recording node has invalid fields, states, time or a duplicate identity.");
      } else {
        ids.add(String(node.id));
      }
    }
    for (const edge of value.edges) {
      if (!is_object(edge) || !fields(edge, ["source", "target", "relation", "evidence"]) || !finite_time(edge.at)
          || !ids.has(String(edge.source)) || !ids.has(String(edge.target))) {
        throw new Error("A causal edge references an absent artifact or has invalid fields.");
      } else {
        continue;
      }
    }
    for (const event of value.events) {
      if (!is_object(event) || !fields(event, ["kind", "actor", "target", "detail"]) || !finite_time(event.at) || !sequence(event.sequence)) {
        throw new Error("A recorded event is invalid.");
      } else if (event.canonical !== undefined && event.canonical !== null
          && (!is_object(event.canonical) || !is_json(event.canonical)
            || !fields(event.canonical, ["local_id", "type", "actor", "role"])
            || event.canonical.type !== event.kind || event.canonical.actor !== event.actor
            || event.canonical.seq !== event.sequence)) {
        throw new Error("A canonical Event envelope is invalid or disagrees with its observation.");
      } else {
        continue;
      }
    }
    for (const session of value.sessions) {
      if (!is_object(session) || !fields(session, ["id", "actor", "task", "kind", "goal"])
          || !finite_time(session.start) || (session.end !== null && (!finite_time(session.end) || session.end < session.start))
          || !ids.has(String(session.task)) || typeof session.actor !== "string" || !value.workers.includes(session.actor)) {
        throw new Error("A session has invalid worker, task or observation bounds.");
      } else {
        continue;
      }
    }
    if (value.run_states !== undefined && (!Array.isArray(value.run_states) || !value.run_states.every((state: unknown) => is_object(state) && finite_time(state.at) && is_object(state.status) && is_json(state.status)))) {
      throw new Error("Recorded run states are invalid.");
    } else if (value.report !== undefined && value.report !== null && (!is_object(value.report) || !is_json(value.report))) {
      throw new Error("The recorded report is invalid.");
    } else if (value.report_at !== undefined && value.report_at !== null && !finite_time(value.report_at)) {
      throw new Error("The report timestamp is invalid.");
    } else if (value.report_error !== undefined && value.report_error !== null && typeof value.report_error !== "string") {
      throw new Error("The report error is invalid.");
    } else {
      return;
    }
  }
}

export function layout_recording(recording: Recording): WorldLayout {
  const points = new Map<string, WorldPoint>();
  const nodes = new Map(recording.nodes.map((node) => [node.id, node]));
  const primary = new Map<string, string>();
  for (const node of recording.nodes) {
    const parents = recording.edges.filter((edge) => edge.target === node.id);
    const preferred = node.kind === "baseline" ? "baseline parent"
      : node.kind === "task" ? "task reference" : node.kind === "candidate" ? "produced" : "evidence";
    const edge = parents.find((candidate) => candidate.relation === preferred) ?? parents.find((candidate) => candidate.relation === "produced") ?? parents[0];
    if (edge) {
      primary.set(node.id, edge.source);
    } else {
      continue;
    }
  }
  const visiting = new Set<string>();
  function place(identity: string): WorldPoint {
    const existing = points.get(identity);
    if (existing) {
      return existing;
    } else if (visiting.has(identity)) {
      throw new Error("The recording's primary causal relationships contain a cycle.");
    } else {
      visiting.add(identity);
      const node = nodes.get(identity);
      if (!node) {
        throw new Error(`Missing spatial artifact ${identity}.`);
      } else {
        const worker_index = recording.workers.indexOf(node.actor);
        const parent_id = primary.get(identity);
        const parent = parent_id ? place(parent_id) : null;
        const angle = worker_index >= 0 ? worker_index * 2.3999632297 - Math.PI / 2
          : parent ? Math.atan2(parent.y, parent.x) : 0;
        const siblings = parent_id ? [...primary].filter(([, source]) => source === parent_id).map(([target]) => target) : [];
        const sibling_index = siblings.indexOf(identity);
        const spread = Math.ceil(sibling_index / 2) * (sibling_index % 2 ? 1 : -1) * 45;
        const point = node.kind === "baseline" ? { x: (parent?.x ?? -120) + 120, y: (parent?.y ?? 30) - 30 }
          : parent ? { x: parent.x + Math.cos(angle) * 125 - Math.sin(angle) * spread, y: parent.y + Math.sin(angle) * 125 + Math.cos(angle) * spread }
            : { x: Math.cos(angle) * (100 + Math.sqrt(worker_index + 1) * 34), y: Math.sin(angle) * (100 + Math.sqrt(worker_index + 1) * 34) };
        visiting.delete(identity);
        points.set(identity, point);
        return point;
      }
    }
  }
  for (const node of recording.nodes) place(node.id);
  for (const [identity, point] of points) {
    points.set(identity, { x: point.x * 0.82 - 220, y: point.y * 0.82 + 120 });
  }
  const approaches = new Map<string, WorldPoint>();
  for (const approach of new Set(recording.nodes.map((node) => node.approach))) {
    const members = recording.nodes.filter((node) => node.approach === approach).map((node) => points.get(node.id)!);
    approaches.set(approach, { x: members.reduce((sum, point) => sum + point.x, 0) / members.length, y: members.reduce((sum, point) => sum + point.y, 0) / members.length });
  }
  const all_points = [...points.values()];
  const left = Math.min(...all_points.map((point) => point.x)) - 180;
  const right = Math.max(...all_points.map((point) => point.x)) + 180;
  const top = Math.min(...all_points.map((point) => point.y)) - 180;
  const bottom = Math.max(...all_points.map((point) => point.y)) + 180;
  return { nodes: points, approaches, bounds: { x: left, y: top, width: right - left, height: bottom - top } };
}
