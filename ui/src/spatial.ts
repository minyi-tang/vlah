export interface WorldPoint { x: number; y: number }
export interface CameraView { x_pixels: number; y_pixels: number; scale: number }
export interface ViewportSize { width_pixels: number; height_pixels: number }
export interface WorldLayout { nodes: ReadonlyMap<string, WorldPoint>; approaches: ReadonlyMap<string, WorldPoint>; bounds: { x: number; y: number; width: number; height: number } }
export type CameraScope = { level: "overview" } | { level: "approach"; id: string } | { level: "node"; id: string };
export const WORLD_BOUNDS = { x: -1140, y: -790, width: 2280, height: 1820 };
export const CAMERA_DURATION_MILLISECONDS = 760;

export function require_point(points: ReadonlyMap<string, WorldPoint>, identity: string): WorldPoint {
  const point = points.get(identity);
  if (!point) {
    throw new Error(`No spatial position for ${identity}.`);
  } else {
    return point;
  }
}

export function camera_for_scope(layout: WorldLayout, scope: CameraScope, viewport: ViewportSize): CameraView {
  const point = scope.level === "overview" ? { x: layout.bounds.x + layout.bounds.width / 2, y: layout.bounds.y + layout.bounds.height / 2 }
    : require_point(scope.level === "approach" ? layout.approaches : layout.nodes, scope.id);
  const width = scope.level === "overview" ? layout.bounds.width : scope.level === "approach" ? 1000 : 430;
  const height = scope.level === "overview" ? layout.bounds.height : scope.level === "approach" ? 850 : 420;
  const scale = Math.min(viewport.width_pixels / width, viewport.height_pixels / height);
  if (!Number.isFinite(scale) || scale <= 0) {
    throw new RangeError("The map viewport must have positive, finite dimensions.");
  } else {
    return { x_pixels: viewport.width_pixels / 2 - point.x * scale, y_pixels: viewport.height_pixels / 2 - point.y * scale, scale };
  }
}

export function zoom_camera(camera: CameraView, anchor: WorldPoint, factor: number, overview_scale: number): CameraView {
  const scale = Math.max(overview_scale * 0.8, Math.min(overview_scale * 18, camera.scale * factor));
  return { x_pixels: anchor.x - (anchor.x - camera.x_pixels) * scale / camera.scale, y_pixels: anchor.y - (anchor.y - camera.y_pixels) * scale / camera.scale, scale };
}

export function interpolate_camera(start: CameraView, target: CameraView, progress: number): CameraView {
  if (progress <= 0) {
    return start;
  } else if (progress >= 1) {
    return target;
  } else {
    const eased = 1 - (1 - progress) ** 3;
    return { x_pixels: start.x_pixels + (target.x_pixels - start.x_pixels) * eased, y_pixels: start.y_pixels + (target.y_pixels - start.y_pixels) * eased, scale: start.scale + (target.scale - start.scale) * eased };
  }
}

export function knowledge_path(start: WorldPoint, end: WorldPoint): string {
  return `M ${start.x} ${start.y} Q ${(start.x + end.x) / 2 + 45} ${(start.y + end.y) / 2 - 75} ${end.x} ${end.y}`;
}

export function point_on_path(start: WorldPoint, end: WorldPoint, progress: number): WorldPoint {
  const phase = Math.max(0, Math.min(1, progress));
  return {
    x: (1 - phase) ** 2 * start.x + 2 * (1 - phase) * phase * ((start.x + end.x) / 2 + 45) + phase ** 2 * end.x,
    y: (1 - phase) ** 2 * start.y + 2 * (1 - phase) * phase * ((start.y + end.y) / 2 - 75) + phase ** 2 * end.y,
  };
}
