import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { HTMLAttributes, RefObject } from "react";

import { CAMERA_DURATION_MILLISECONDS, camera_for_scope, interpolate_camera, zoom_camera } from "./spatial.ts";
import type { CameraScope, CameraView, ViewportSize, WorldLayout, WorldPoint } from "./spatial.ts";

interface WorldCameraControls {
  viewport_ref: RefObject<HTMLDivElement | null>;
  viewport: ViewportSize;
  camera: CameraView;
  relative_zoom: number;
  zoom: (factor: number) => void;
  gestures: Pick<HTMLAttributes<HTMLDivElement>, "onPointerDown" | "onPointerMove" | "onPointerUp" | "onPointerCancel" | "onClickCapture" | "onKeyDown">;
}

export function useWorldCamera(layout: WorldLayout, scope: CameraScope, on_navigate: (scope: CameraScope) => void): WorldCameraControls {
  const viewport_ref = useRef<HTMLDivElement>(null);
  const [viewport, set_viewport] = useState<ViewportSize>({ width_pixels: 0, height_pixels: 0 });
  const [camera, set_camera] = useState<CameraView>({ x_pixels: 0, y_pixels: 0, scale: 1 });
  const camera_ref = useRef(camera);
  const overview_scale_ref = useRef(1);
  const animation_frame = useRef(0);
  const has_initialized = useRef(false);
  const reduced_motion = useRef(window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  const pointers = useRef(new Map<number, WorldPoint>());
  const has_dragged = useRef(false);
  const drag_distance_pixels = useRef(0);
  const overview = useMemo(() => viewport.width_pixels && viewport.height_pixels
    ? camera_for_scope(layout, { level: "overview" }, viewport) : null, [layout, viewport]);
  const target = useMemo(() => overview ? camera_for_scope(layout, scope, viewport) : null, [layout, scope, viewport, overview]);

  const move_camera = useCallback((next_camera: CameraView, animate: boolean): void => {
    window.cancelAnimationFrame(animation_frame.current);
    if (!animate || reduced_motion.current) {
      camera_ref.current = next_camera;
      set_camera(next_camera);
    } else {
      const start_camera = camera_ref.current;
      let start_milliseconds: number | null = null;
      function advance(timestamp_milliseconds: number): void {
        start_milliseconds ??= timestamp_milliseconds;
        const progress = Math.min(1, (timestamp_milliseconds - start_milliseconds) / CAMERA_DURATION_MILLISECONDS);
        const next_frame = interpolate_camera(start_camera, next_camera, progress);
        camera_ref.current = next_frame;
        set_camera(next_frame);
        if (progress < 1) {
          animation_frame.current = window.requestAnimationFrame(advance);
        } else {
          animation_frame.current = 0;
        }
      }
      animation_frame.current = window.requestAnimationFrame(advance);
    }
  }, []);

  useLayoutEffect(() => {
    const element = viewport_ref.current;
    if (!element) {
      return;
    } else {
      const resize = (): void => set_viewport({ width_pixels: element.clientWidth, height_pixels: element.clientHeight });
      resize();
      const observer = new ResizeObserver(resize);
      observer.observe(element);
      return () => observer.disconnect();
    }
  }, []);

  const target_x = target?.x_pixels;
  const target_y = target?.y_pixels;
  const target_scale = target?.scale;
  useLayoutEffect(() => {
    if (target_x === undefined || target_y === undefined || target_scale === undefined) {
      return;
    } else {
      move_camera({ x_pixels: target_x, y_pixels: target_y, scale: target_scale }, has_initialized.current);
      has_initialized.current = true;
    }
  }, [scope, target_x, target_y, target_scale, move_camera]);

  useLayoutEffect(() => {
    if (overview) {
      overview_scale_ref.current = overview.scale;
    } else {
      return;
    }
  }, [overview]);

  useEffect(() => {
    const preference = window.matchMedia("(prefers-reduced-motion: reduce)");
    const change = (): void => {
      reduced_motion.current = preference.matches;
      if (preference.matches) {
        window.cancelAnimationFrame(animation_frame.current);
      } else {
        return;
      }
    };
    preference.addEventListener("change", change);
    return () => {
      preference.removeEventListener("change", change);
      window.cancelAnimationFrame(animation_frame.current);
    };
  }, []);

  useEffect(() => {
    const element = viewport_ref.current;
    if (!element) {
      return;
    } else {
      const wheel = (event: WheelEvent): void => {
        if (event.target instanceof Element && event.target.closest(".world-lens")) {
          return;
        } else {
          event.preventDefault();
          const bounds = element.getBoundingClientRect();
          const delta_pixels = event.deltaY * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? bounds.height : 1);
          move_camera(zoom_camera(camera_ref.current, { x: event.clientX - bounds.left, y: event.clientY - bounds.top }, Math.exp(-delta_pixels * 0.002), overview_scale_ref.current), false);
        };
      }
      element.addEventListener("wheel", wheel, { passive: false });
      return () => element.removeEventListener("wheel", wheel);
    }
  }, [move_camera]);

  function zoom(factor: number): void {
    move_camera(zoom_camera(camera_ref.current, { x: viewport.width_pixels / 2, y: viewport.height_pixels / 2 }, factor, overview_scale_ref.current), true);
  }

  const gestures: WorldCameraControls["gestures"] = {
    onPointerDown: (event) => {
      has_dragged.current = pointers.current.size > 0;
      drag_distance_pixels.current = 0;
      if (event.button !== 0 || (pointers.current.size === 0 && event.target instanceof Element && event.target.closest("button, .world-lens"))) {
        return;
      } else {
        event.currentTarget.focus({ preventScroll: true });
        window.cancelAnimationFrame(animation_frame.current);
        event.currentTarget.setPointerCapture(event.pointerId);
        const bounds = event.currentTarget.getBoundingClientRect();
        pointers.current.set(event.pointerId, { x: event.clientX - bounds.left, y: event.clientY - bounds.top });
      }
    },
    onPointerMove: (event) => {
      const previous = pointers.current.get(event.pointerId);
      if (!previous) {
        return;
      } else {
        const bounds = event.currentTarget.getBoundingClientRect();
        const next = { x: event.clientX - bounds.left, y: event.clientY - bounds.top };
        const partner = [...pointers.current.entries()].find(([identity]) => identity !== event.pointerId)?.[1];
        const delta_x = next.x - previous.x;
        const delta_y = next.y - previous.y;
        drag_distance_pixels.current += Math.hypot(delta_x, delta_y);
        has_dragged.current ||= drag_distance_pixels.current > 4;
        pointers.current.set(event.pointerId, next);
        if (partner) {
          const previous_distance = Math.max(1, Math.hypot(previous.x - partner.x, previous.y - partner.y));
          const next_distance = Math.max(1, Math.hypot(next.x - partner.x, next.y - partner.y));
          const next_camera = zoom_camera(camera_ref.current, { x: (previous.x + partner.x) / 2, y: (previous.y + partner.y) / 2 }, next_distance / previous_distance, overview_scale_ref.current);
          move_camera({ ...next_camera, x_pixels: next_camera.x_pixels + delta_x / 2, y_pixels: next_camera.y_pixels + delta_y / 2 }, false);
        } else {
          move_camera({ ...camera_ref.current, x_pixels: camera_ref.current.x_pixels + delta_x, y_pixels: camera_ref.current.y_pixels + delta_y }, false);
        }
      }
    },
    onPointerUp: (event) => { pointers.current.delete(event.pointerId); },
    onPointerCancel: (event) => { pointers.current.delete(event.pointerId); },
    onClickCapture: (event) => {
      if (has_dragged.current) {
        event.preventDefault();
        event.stopPropagation();
        has_dragged.current = false;
      } else {
        return;
      }
    },
    onKeyDown: (event) => {
      if (event.target !== event.currentTarget) {
        return;
      } else {
        switch (event.key) {
          case "+": case "=": event.preventDefault(); zoom(1.5); break;
          case "-": event.preventDefault(); zoom(1 / 1.5); break;
          case "Home": event.preventDefault(); on_navigate({ level: "overview" }); break;
          case "Escape":
            event.preventDefault();
            on_navigate({ level: "overview" });
            break;
          case "ArrowLeft": case "ArrowRight": case "ArrowUp": case "ArrowDown":
            event.preventDefault();
            move_camera({ ...camera_ref.current,
              x_pixels: camera_ref.current.x_pixels + (event.key === "ArrowLeft" ? 80 : event.key === "ArrowRight" ? -80 : 0),
              y_pixels: camera_ref.current.y_pixels + (event.key === "ArrowUp" ? 80 : event.key === "ArrowDown" ? -80 : 0),
            }, true);
            break;
          default: break;
        }
      }
    },
  };
  return { viewport_ref, viewport, camera, relative_zoom: overview ? camera.scale / overview.scale : 1, zoom, gestures };
}
