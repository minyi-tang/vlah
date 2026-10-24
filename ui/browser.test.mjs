import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";
import { createServer } from "vite";

import { RECORDING } from "./src/recording.ts";

const root = fileURLToPath(new URL(".", import.meta.url));
const server = await createServer({ root, configFile: `${root}/vite.config.ts`, server: { host: "127.0.0.1", port: 0, hmr: false } });
await server.listen();
const address = server.httpServer.address();
const url = `http://127.0.0.1:${address.port}`;
const browser = await chromium.launch({ headless: true, ...(process.env.PLAYWRIGHT_CHANNEL ? { channel: process.env.PLAYWRIGHT_CHANNEL } : {}) });
const errors = [];
const checks = [];
const artifacts = process.env.UI_ARTIFACTS_DIR;
if (artifacts) await mkdir(artifacts, { recursive: true });

async function seek(page, seconds) {
  await page.getByRole("slider", { name: "Replay position" }).evaluate((input, value) => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(input, String(value));
    input.dispatchEvent(new Event("input", { bubbles: true }));
  }, seconds);
  await page.waitForTimeout(50);
}
async function screenshot(page, name) {
  if (artifacts) await page.screenshot({ path: `${artifacts}/${name}.png` });
}
function partial_recording(time) {
  const nodes = RECORDING.nodes.filter(node => node.at <= time).map(node => ({ ...node, states: node.states.filter(state => state.at <= time) }));
  const ids = new Set(nodes.map(node => node.id));
  return { ...RECORDING, duration_seconds: time, nodes, edges: RECORDING.edges.filter(edge => edge.at <= time && ids.has(edge.source) && ids.has(edge.target)), events: RECORDING.events.filter(event => event.at <= time), sessions: RECORDING.sessions.filter(session => session.start <= time).map(session => ({ ...session, end: session.end !== null && session.end <= time ? session.end : null })), run_states: RECORDING.run_states.filter(state => state.at <= time), report: RECORDING.report_at <= time ? RECORDING.report : null, report_at: RECORDING.report_at <= time ? RECORDING.report_at : null };
}

try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  page.setDefaultTimeout(7000);
  page.on("pageerror", error => errors.push(error.message));
  await page.goto(url, { waitUntil: "networkidle" });
  await page.getByRole("button", { name: "Pause replay", exact: true }).click();
  assert.equal(await page.locator("[data-worker-id]").count(), 101);
  const early_nodes = await page.locator("[data-node-id]").count();
  await seek(page, 29);
  assert.ok(await page.locator("[data-node-id]").count() > early_nodes);
  assert.equal(await page.locator("[data-worker-id]").count(), 101);
  await screenshot(page, "causal-overview");
  checks.push("Recorded artifacts grow while 100 worker identities and the compose slot stay fixed");

  await seek(page, RECORDING.duration_seconds);
  const child = RECORDING.nodes.find(node => RECORDING.edges.filter(edge => edge.target === node.id && edge.relation === "candidate parent").length > 1);
  assert.ok(child);
  await page.getByRole("button", { name: `Inspect ${child.title}: ${child.id}`, exact: true }).click();
  await page.waitForTimeout(180);
  const intermediate = Number(await page.locator(".causal-plane").getAttribute("data-camera-scale"));
  await page.waitForTimeout(800);
  const final_scale = Number(await page.locator(".causal-plane").getAttribute("data-camera-scale"));
  assert.ok(final_scale > intermediate);
  await page.getByRole("tab", { name: "links", exact: true }).click();
  assert.ok(await page.locator(".trace-link").filter({ hasText: "candidate parent" }).count() >= 2);
  await screenshot(page, "causal-lineage");
  await page.getByRole("tab", { name: "links", exact: true }).press("ArrowLeft");
  assert.equal(await page.getByRole("tab", { name: "history", exact: true }).getAttribute("aria-selected"), "true");
  checks.push("Smooth zoom and an inspector expose actual multi-parent lineage and source fields");

  await page.getByRole("button", { name: "Event trail", exact: true }).click();
  await page.locator(".trace-event-row").filter({ hasText: "audit checked" }).first().click();
  assert.match(await page.locator(".canonical-event").innerText(), /"threshold"/);
  assert.match(await page.locator(".canonical-event").innerText(), /"local_id"/);
  await page.getByRole("button", { name: "Run report", exact: true }).click();
  assert.match(await page.locator(".drawer-run").innerText(), /"worker_count": 100/);
  assert.match(await page.locator(".drawer-run").innerText(), /"holdout_score": null/);
  await seek(page, 0);
  assert.match(await page.locator(".drawer-run").innerText(), /No report was captured/);
  await seek(page, RECORDING.duration_seconds);
  checks.push("Events without graph nodes retain their full envelope, and reports respect replay time");

  await page.getByRole("textbox", { name: "Find a worker" }).fill("worker-100");
  assert.equal(await page.locator(".trace-worker-row").count(), 1);
  await page.locator(".trace-worker-row").click();
  await page.waitForTimeout(850);
  assert.match(await page.locator(".trace-inspector").innerText(), /worker-100/);
  assert.match(await page.locator(".trace-inspector .trace-goal").innerText(), /Advance the whole problem/);
  checks.push("The hundredth worker remains searchable and shows its recorded goal, not invented activity");

  await page.getByRole("button", { name: "Show whole universe" }).click();
  await page.waitForTimeout(850);
  const bounds = await page.locator(".causal-viewport").boundingBox();
  const before = Number(await page.locator(".causal-plane").getAttribute("data-camera-scale"));
  await page.mouse.move(bounds.x + bounds.width * 0.65, bounds.y + bounds.height * 0.35);
  await page.mouse.wheel(0, -140);
  await page.waitForTimeout(100);
  assert.ok(Number(await page.locator(".causal-plane").getAttribute("data-camera-scale")) > before);
  await seek(page, 0);
  assert.equal(await page.locator('[data-node-kind="candidate"]').count(), 0);
  assert.equal(await page.locator("[data-worker-id]").count(), 101);
  checks.push("Wheel zoom works and rewind removes future work without deleting workers");

  let served = partial_recording(20);
  let response_status = 200;
  await page.route("**/recording.json", route => route.fulfill({ status: response_status, contentType: "application/json", body: JSON.stringify(served) }));
  await page.getByRole("button", { name: "Provenance", exact: true }).click();
  await page.getByRole("button", { name: "Follow recording.json", exact: true }).click();
  await page.waitForFunction(() => document.querySelector(".replay-source").textContent.includes("FOLLOWING"));
  await page.waitForTimeout(150);
  await page.getByRole("textbox", { name: "Find a worker" }).fill("worker-100");
  await page.locator(".trace-worker-row").click();
  served = RECORDING;
  await page.waitForFunction(duration => Number(document.querySelector('[aria-label="Replay position"]').value) === duration, RECORDING.duration_seconds, { timeout: 5000 });
  assert.match(await page.locator(".trace-inspector").innerText(), /worker-100/);
  response_status = 503;
  await page.getByRole("alert").waitFor({ timeout: 5000 });
  assert.equal(await page.locator("[data-worker-id]").count(), 101);
  checks.push("File following preserves inspection and surfaces transport failures with the last valid data intact");

  await page.locator('input[type="file"]').setInputFiles({ name: "invalid.json", mimeType: "application/json", buffer: Buffer.from("{}") });
  assert.match(await page.getByRole("alert").innerText(), /Could not open/);
  await page.locator('input[type="file"]').setInputFiles({ name: "recorded.json", mimeType: "application/json", buffer: Buffer.from(JSON.stringify(RECORDING)) });
  await page.getByRole("alert").waitFor({ state: "hidden" });
  checks.push("Recording imports validate their schema instead of silently falling back");

  const narrow = await browser.newPage({ viewport: { width: 390, height: 844 }, reducedMotion: "reduce", hasTouch: true });
  narrow.setDefaultTimeout(7000);
  narrow.on("pageerror", error => errors.push(error.message));
  await narrow.goto(url, { waitUntil: "networkidle" });
  assert.equal(await narrow.getByRole("button", { name: "Play replay", exact: true }).count(), 1);
  await seek(narrow, RECORDING.duration_seconds);
  await narrow.getByRole("textbox", { name: "Find a worker" }).fill("worker-100");
  await narrow.locator(".trace-worker-row").click();
  assert.equal(await narrow.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  await screenshot(narrow, "causal-narrow");
  await narrow.getByRole("button", { name: "Show whole universe" }).click();
  const touch_bounds = await narrow.locator(".causal-viewport").boundingBox();
  const point = await narrow.evaluate(box => {
    for (const fraction of [0.15, 0.8, 0.65, 0.4]) {
      const x = box.x + box.width * 0.6;
      const y = box.y + box.height * fraction;
      if ([x - 20, x + 20].every(value => !document.elementFromPoint(value, y)?.closest("button"))) return { x, y };
    }
    return null;
  }, touch_bounds);
  assert.ok(point);
  const prior_scale = Number(await narrow.locator(".causal-plane").getAttribute("data-camera-scale"));
  const session = await narrow.context().newCDPSession(narrow);
  await session.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ x: point.x - 20, y: point.y, id: 1 }, { x: point.x + 20, y: point.y, id: 2 }] });
  await session.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: [{ x: point.x - 45, y: point.y, id: 1 }, { x: point.x + 45, y: point.y, id: 2 }] });
  await session.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
  await narrow.waitForTimeout(100);
  assert.ok(Number(await narrow.locator(".causal-plane").getAttribute("data-camera-scale")) > prior_scale * 1.5);
  checks.push("Narrow screens, keyboard tabs, reduced motion and real two-touch pinch remain usable");
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ checks, console_errors: errors }, null, 2));
} finally {
  await browser.close();
  await server.close();
}
