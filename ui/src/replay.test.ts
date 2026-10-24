import assert from "node:assert/strict";
import { test } from "node:test";

import { RECORDING } from "./recording.ts";
import { event_by_id, event_id, event_target, layout_recording, node_by_id, session_at, state_at, validate_recording, visible_graph, worker_node, worker_point } from "./replay.ts";
import type { Recording } from "./replay.ts";
import { camera_for_scope, interpolate_camera, require_point, zoom_camera } from "./spatial.ts";

const recording: Recording = RECORDING;

test("the generated recording is valid and never creates worker descendants", () => {
  validate_recording(recording);
  assert.equal(recording.workers.length, 101);
  assert.equal(recording.workers.filter((worker) => worker !== recording.compose_actor).length, 100);
  assert.ok(recording.events.every((event) => event.kind !== "spawn"));
  const ids = new Set(recording.nodes.map((node) => node.id));
  for (const edge of recording.edges) {
    assert.ok(ids.has(edge.source) && ids.has(edge.target));
    assert.ok(edge.evidence.length > 0);
  }
});

test("new recorded work forks and multi-parent candidates and baselines join it", () => {
  const early = visible_graph(recording, 0);
  const final = visible_graph(recording, recording.duration_seconds);
  assert.ok(final.nodes.length > early.nodes.length);
  assert.ok(recording.nodes.some((node) => recording.edges.filter((edge) => edge.target === node.id && edge.relation === "candidate parent").length > 1));
  assert.ok(recording.nodes.some((node) => recording.edges.filter((edge) => edge.source === node.id && edge.relation === "task reference").length > 1));
  assert.ok(final.edges.some((edge) => edge.relation === "integrated"));
  assert.ok(final.edges.some((edge) => edge.relation === "baseline parent"));
});

test("rewind removes future artifacts and references without changing the recording", () => {
  const original = JSON.stringify(recording);
  for (const time of [0, 6, 18, 27, 40, recording.duration_seconds, 0]) {
    const graph = visible_graph(recording, time);
    const ids = new Set(graph.nodes.map((node) => node.id));
    assert.ok(graph.nodes.every((node) => node.at <= time));
    assert.ok(graph.edges.every((edge) => edge.at <= time && ids.has(edge.source) && ids.has(edge.target)));
  }
  assert.equal(JSON.stringify(recording), original);
});

test("a task ending is not a worker birth or a judge verdict", () => {
  const worker = "worker-01";
  const sessions = recording.sessions.filter((session) => session.actor === worker);
  assert.ok(sessions.length > 1);
  const first = sessions[0];
  assert.equal(session_at(recording, worker, first.start)?.task, first.task);
  assert.notEqual(state_at(node_by_id(recording, first.task), first.end!), "integrated");
  assert.equal(state_at(node_by_id(recording, first.task), first.end!), "session ended");
  assert.ok(sessions.some((session) => session.kind === "result_followup"));
  assert.ok(worker_node(recording, "worker-100", recording.duration_seconds));
});

test("failed integration retains its artifact and creates recorded follow-up work", () => {
  const failures = recording.events.filter((event) => event.kind === "integration_failed");
  assert.ok(failures.length > 0);
  for (const failure of failures) {
    const node = node_by_id(recording, failure.target);
    assert.equal(state_at(node, recording.duration_seconds), "integration failed");
    assert.ok(visible_graph(recording, recording.duration_seconds).nodes.includes(node));
    assert.ok(recording.edges.some((edge) => edge.source === node.id && edge.relation === "task reference"));
  }
});

test("spatial positions stay stable while recording time changes and every actor is reachable", () => {
  const layout = layout_recording(recording);
  for (const time of [0, 10, 30, recording.duration_seconds]) {
    for (const node of visible_graph(recording, time).nodes) {
      const point = require_point(layout.nodes, node.id);
      assert.ok(Number.isFinite(point.x) && Number.isFinite(point.y));
    }
  }
  for (const worker of recording.workers) {
    const identity = worker_node(recording, worker, recording.duration_seconds);
    assert.ok(identity);
    assert.ok(layout.nodes.has(identity));
  }
});

test("camera interpolation preserves continuity and zoom anchors under the pointer", () => {
  const layout = layout_recording(recording);
  const viewport = { width_pixels: 1100, height_pixels: 620 };
  const start = camera_for_scope(layout, { level: "overview" }, viewport);
  const target = camera_for_scope(layout, { level: "node", id: recording.nodes.at(-1)!.id }, viewport);
  const halfway = interpolate_camera(start, target, 0.5);
  assert.deepEqual(interpolate_camera(start, target, 0), start);
  assert.deepEqual(interpolate_camera(start, target, 1), target);
  assert.deepEqual(interpolate_camera(halfway, start, 0), halfway);
  const anchor = { x: 420, y: 210 };
  const world_x = (anchor.x - start.x_pixels) / start.scale;
  const world_y = (anchor.y - start.y_pixels) / start.scale;
  for (const factor of [0.0001, 0.8, 2, 1000]) {
    const next = zoom_camera(start, anchor, factor, start.scale);
    assert.ok(Math.abs(next.x_pixels + world_x * next.scale - anchor.x) < 1e-8);
    assert.ok(Math.abs(next.y_pixels + world_y * next.scale - anchor.y) < 1e-8);
  }
});

test("malformed imports fail explicitly rather than falling back to a demo", () => {
  assert.throws(() => validate_recording({}), /Expected a VLAH recording/);
  assert.throws(() => validate_recording({ ...recording, nodes: [...recording.nodes, recording.nodes[0]] }), /duplicate identity/);
  assert.throws(() => validate_recording({ ...recording, edges: [{ source: "missing", target: recording.nodes[0].id, relation: "fake", evidence: "", at: 0 }] }), /absent artifact/);
  assert.throws(() => validate_recording({ ...recording, sessions: [{ ...recording.sessions[0], end: -1 }] }), /observation bounds/);
});

test("worker motion and the historical layout do not jump at a new contribution", () => {
  const layout = layout_recording(recording);
  const contribution = recording.nodes.find((node) => node.actor === "worker-01" && node.kind === "candidate");
  assert.ok(contribution);
  const before = worker_point(recording, layout, "worker-01", contribution.at - 0.001);
  const arrived = worker_point(recording, layout, "worker-01", contribution.at);
  assert.deepEqual(arrived, before);
  const next = worker_point(recording, layout, "worker-01", contribution.at + 1 / 60);
  assert.ok(Math.hypot(next.x - arrived.x, next.y - arrived.y) < 30);
  const early_ids = new Set(recording.nodes.filter((node) => node.at <= 18).map((node) => node.id));
  const partial: Recording = { ...recording, nodes: recording.nodes.filter((node) => early_ids.has(node.id)), edges: recording.edges.filter((edge) => early_ids.has(edge.source) && early_ids.has(edge.target)) };
  const early_layout = layout_recording(partial);
  for (const [identity, point] of early_layout.nodes) assert.deepEqual(layout.nodes.get(identity), point);
});

test("canonical envelopes and reports are retained without manufacturing artifact nodes", () => {
  for (const event of recording.events) {
    assert.ok(event.canonical);
    assert.equal(event.canonical.type, event.kind);
    assert.equal(event.canonical.seq, event.sequence);
    assert.ok("from" in event.canonical && "to" in event.canonical);
    assert.deepEqual(event_by_id(recording, event_id(event)), event);
  }
  const audit = recording.events.find((event) => event.kind === "audit_checked");
  assert.ok(audit);
  assert.equal(event_target(recording, audit), null);
  assert.ok(audit.canonical?.data);
  assert.ok(recording.report && recording.report_at !== null);
  assert.equal(recording.run_states?.at(-1)?.status.is_running, false);
  assert.ok(JSON.stringify(recording.report).includes('"holdout_score":null'));
});
