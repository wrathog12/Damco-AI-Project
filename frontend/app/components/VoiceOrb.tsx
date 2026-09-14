"use client";

import { useEffect, useRef } from "react";
import type { RefObject } from "react";
import { cn } from "@/lib/cn";

export type OrbState =
  | "idle"
  | "connecting"
  | "listening"
  | "thinking"
  | "speaking";

/**
 * The orb.
 *
 * Three things about this are deliberate, and each one is a fix for how v1 did
 * it.
 *
 * **The animation loop starts once and never restarts.** It reads the current
 * loudness out of `level.current` and the current mode out of `mode.current`, so
 * neither a new audio sample nor a state change touches the effect. v1's loop
 * was torn down and rebuilt on every amplitude update — roughly sixty times a
 * second while the mic was live — which also resized the canvas sixty times a
 * second.
 *
 * **Two loudness sources, not one.** v1 read the local microphone only, so the
 * orb sat perfectly still for the entire time the agent was talking, which is
 * most of a call. Whoever holds the floor drives it: the caller's mic while
 * listening, the bot's track while speaking.
 *
 * **Shapes, not particles.** v1 called `arc()` 260 times per frame to draw 260
 * one-pixel dots. This draws three smooth closed paths of 72 points each and one
 * gradient, which is fewer canvas operations for a result that reads as a living
 * thing rather than static.
 *
 * Sizing comes from a ResizeObserver rather than a `size` prop, so the component
 * is responsive without the parent computing pixels — and so a resize does not
 * have to invalidate anything.
 */
interface VoiceOrbProps {
  /** Loudness, 0-1, updated out-of-band. See `useAudioLevel`. */
  level: RefObject<number>;
  state: OrbState;
  className?: string;
}

/** Which token drives the colour in each mode. Listening is the warm one: it is
 *  the only moment the caller is being asked to act, and the accent is the only
 *  warm colour in the palette precisely so it can mean that. */
const MODE_TOKEN: Record<OrbState, "--primary" | "--accent" | "--ink-subtle"> = {
  idle: "--ink-subtle",
  connecting: "--primary",
  listening: "--accent",
  thinking: "--primary",
  speaking: "--primary",
};

/** How much of the orb's radius the loudness reading is allowed to move. */
const MODE_REACTIVITY: Record<OrbState, number> = {
  idle: 0,
  connecting: 0.02,
  listening: 0.16,
  thinking: 0.05,
  speaking: 0.2,
};

export default function VoiceOrb({ level, state, className }: VoiceOrbProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  // The loop reads these instead of closing over props, which is what lets the
  // effect below have an empty dependency list.
  const mode = useRef<OrbState>(state);
  const colour = useRef<[number, number, number]>([120, 120, 140]);
  // A ref *to* the level ref. The caller swaps which track drives the orb —
  // their own mic while listening, the bot's while it speaks — and the loop
  // closed over its arguments at mount, so it has to be able to see the swap.
  const source = useRef(level);

  // Assigned in an effect rather than during render. A ref write during render
  // is not permitted — under concurrent rendering the render that wrote it need
  // not be the one that commits — and the loop reads these asynchronously, so
  // landing them a frame after the paint is not observable.
  useEffect(() => {
    mode.current = state;
    source.current = level;
  });

  /* Resolve the CSS token to concrete RGB whenever the mode or the theme
     changes. Doing this per frame would mean a `getComputedStyle` — a forced
     style recalculation — sixty times a second. */
  useEffect(() => {
    const read = () => {
      const probe = document.createElement("span");
      probe.style.color = `var(${MODE_TOKEN[state]})`;
      probe.style.position = "absolute";
      probe.style.opacity = "0";
      document.body.appendChild(probe);
      const computed = getComputedStyle(probe).color;
      probe.remove();
      const parsed = computed.match(/[\d.]+/g);
      if (parsed && parsed.length >= 3) {
        colour.current = [
          Number(parsed[0]),
          Number(parsed[1]),
          Number(parsed[2]),
        ];
      }
    };
    read();

    // The theme toggle mutates the class on <html>; the token then resolves to a
    // different colour and the orb has to be told.
    const observer = new MutationObserver(read);
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["class"],
    });
    return () => observer.disconnect();
  }, [state]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    let width = 0;
    let height = 0;
    // Capped at 2. A 3x-DPR phone would otherwise paint nine times the pixels
    // for a blurry blob nobody can see the difference in, on the slowest GPU we
    // are targeting.
    const dpr = Math.min(window.devicePixelRatio || 1, 2);

    const resize = () => {
      const rect = canvas.getBoundingClientRect();
      width = rect.width;
      height = rect.height;
      canvas.width = Math.round(width * dpr);
      canvas.height = Math.round(height * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    };
    resize();

    const observer = new ResizeObserver(resize);
    observer.observe(canvas);

    // Smoothed separately from the analyser: the analyser's own smoothing stops
    // the number jittering, this stops the *shape* snapping when a turn starts.
    let smoothed = 0;
    let raf = 0;
    const started = performance.now();

    /** One closed, wobbling ring. `harmonic` and `phase` differ per ring so the
     *  three never line up and the whole thing reads as organic. */
    const ring = (
      cx: number,
      cy: number,
      radius: number,
      wobble: number,
      harmonic: number,
      phase: number,
      alpha: number,
    ) => {
      const [r, g, b] = colour.current;
      const POINTS = 72;
      ctx.beginPath();
      for (let i = 0; i <= POINTS; i++) {
        const theta = (i / POINTS) * Math.PI * 2;
        const offset =
          Math.sin(theta * harmonic + phase) * wobble +
          Math.sin(theta * (harmonic + 2) - phase * 1.4) * wobble * 0.45;
        const rr = radius + offset;
        const x = cx + Math.cos(theta) * rr;
        const y = cy + Math.sin(theta) * rr;
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      }
      ctx.closePath();

      const gradient = ctx.createLinearGradient(
        cx - radius,
        cy - radius,
        cx + radius,
        cy + radius,
      );
      gradient.addColorStop(0, `rgba(${r},${g},${b},${alpha})`);
      gradient.addColorStop(1, `rgba(${r},${g},${b},${alpha * 0.35})`);
      ctx.fillStyle = gradient;
      ctx.fill();
    };

    const frame = (now: number) => {
      const t = (now - started) / 1000;
      const current = mode.current;
      const [r, g, b] = colour.current;

      const target = current === "idle" ? 0 : source.current.current;
      // Asymmetric easing: rise fast so the orb answers a voice immediately,
      // fall slowly so it does not flicker between syllables.
      smoothed += (target - smoothed) * (target > smoothed ? 0.35 : 0.08);

      ctx.clearRect(0, 0, width, height);

      const cx = width / 2;
      const cy = height / 2;
      const base = Math.min(width, height) * 0.29;
      const reactivity = MODE_REACTIVITY[current];
      const breath =
        still || current === "idle"
          ? 0
          : Math.sin(t * (current === "thinking" ? 3.4 : 1.5)) * 0.022;
      const radius = base * (1 + breath + smoothed * reactivity);

      // Outer glow. Grows with loudness so a loud room feels louder without the
      // silhouette itself jumping around.
      const glow = ctx.createRadialGradient(cx, cy, radius * 0.55, cx, cy, radius * 2.1);
      glow.addColorStop(0, `rgba(${r},${g},${b},${0.2 + smoothed * 0.26})`);
      glow.addColorStop(1, `rgba(${r},${g},${b},0)`);
      ctx.fillStyle = glow;
      ctx.fillRect(0, 0, width, height);

      const spin = still ? 0 : t;
      const wobble = radius * (0.035 + smoothed * 0.1);
      ring(cx, cy, radius * 1.16, wobble * 1.5, 3, spin * 0.55, 0.16);
      ring(cx, cy, radius * 1.06, wobble * 1.2, 4, -spin * 0.8 + 1.2, 0.22);
      ring(cx, cy, radius, wobble, 5, spin * 1.15 + 2.6, 0.34);

      // The core. Solid enough to be the focal point at any size, with a
      // top-left highlight so it reads as a sphere and not a disc.
      const core = ctx.createRadialGradient(
        cx - radius * 0.3,
        cy - radius * 0.34,
        radius * 0.05,
        cx,
        cy,
        radius * 0.94,
      );
      core.addColorStop(0, `rgba(255,255,255,0.5)`);
      core.addColorStop(0.32, `rgba(${r},${g},${b},0.96)`);
      core.addColorStop(1, `rgba(${r},${g},${b},0.72)`);
      ctx.beginPath();
      ctx.arc(cx, cy, radius * 0.84, 0, Math.PI * 2);
      ctx.fillStyle = core;
      ctx.fill();

      if (!still) raf = requestAnimationFrame(frame);
    };

    // One frame is enough when motion is off — the orb still shows the mode
    // colour and the loudness at the moment it painted.
    raf = requestAnimationFrame(frame);

    return () => {
      cancelAnimationFrame(raf);
      observer.disconnect();
    };
    // Empty on purpose. Everything that varies is read through a ref, which is
    // the fix for v1's restart-per-frame bug; adding `state` here would
    // reintroduce it.
  }, []);

  return (
    <canvas
      ref={canvasRef}
      // The orb is decoration; the state it represents is announced in text
      // beside it, so a screen reader that also read this out would say
      // everything twice.
      aria-hidden
      className={cn("size-full", className)}
    />
  );
}
