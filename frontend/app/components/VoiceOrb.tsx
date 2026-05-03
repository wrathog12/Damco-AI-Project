"use client";

import { useRef, useEffect, useCallback } from "react";

interface VoiceOrbProps {
  amplitude: number;       // 0‑1 normalised loudness
  isActive: boolean;       // true when mic / voice is streaming
  status: "standby" | "listening" | "processing" | "speaking";
  size?: number;           // canvas CSS px (default 380)
}

// ── Tunables ───────────────────────────────────────────
const PARTICLE_COUNT   = 260;
const CORE_RADIUS      = 0.28;   // fraction of half‑size
const RING_MIN         = 0.32;
const RING_MAX         = 0.75;
const BREATH_SPEED     = 0.004;
const ROTATION_SPEED   = 0.0003;
const AMPLITUDE_SMOOTH = 0.12;   // how fast amp responds
const MAX_DISPLACEMENT = 50;     // px extra radius on max amp

// Colour palette (blue / indigo theme)
const PALETTE = [
  [59, 130, 246],   // blue-500
  [99, 102, 241],   // indigo-500
  [147, 197, 253],  // blue-300
  [129, 140, 248],  // indigo-400
  [96, 165, 250],   // blue-400
  [199, 210, 254],  // indigo-200
  [56, 189, 248],   // sky-400
];

interface Particle {
  angle: number;
  radius: number;   // fraction (RING_MIN … RING_MAX)
  speed: number;
  size: number;
  color: number[];
  opacity: number;
  phaseOffset: number;
}

function makeParticles(): Particle[] {
  const out: Particle[] = [];
  for (let i = 0; i < PARTICLE_COUNT; i++) {
    out.push({
      angle:       Math.random() * Math.PI * 2,
      radius:      RING_MIN + Math.random() * (RING_MAX - RING_MIN),
      speed:       0.0004 + Math.random() * 0.0012,
      size:        1.2 + Math.random() * 2.8,
      color:       PALETTE[Math.floor(Math.random() * PALETTE.length)],
      opacity:     0.25 + Math.random() * 0.55,
      phaseOffset: Math.random() * Math.PI * 2,
    });
  }
  return out;
}

export default function VoiceOrb({
  amplitude,
  isActive,
  status,
  size = 380,
}: VoiceOrbProps) {
  const canvasRef   = useRef<HTMLCanvasElement>(null);
  const particles   = useRef<Particle[]>(makeParticles());
  const frameId     = useRef<number>(0);
  const smoothAmp   = useRef(0);
  const time        = useRef(0);

  /* ---- animation loop ---- */
  const draw = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d")!;
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.width  / dpr;
    const h = canvas.height / dpr;
    const cx = w / 2;
    const cy = h / 2;
    const half = Math.min(cx, cy);

    time.current += 1;

    // smooth amplitude
    const target = isActive ? amplitude : 0;
    smoothAmp.current += (target - smoothAmp.current) * AMPLITUDE_SMOOTH;
    const amp = smoothAmp.current;

    ctx.clearRect(0, 0, w, h);

    // ── 1. Core glow ──────────────────────────────────
    const coreR = half * CORE_RADIUS * (1 + amp * 0.2);
    const breathScale = 1 + Math.sin(time.current * BREATH_SPEED) * 0.04;
    const finalCoreR = coreR * breathScale;

    // outer glow
    const glowGrad = ctx.createRadialGradient(cx, cy, finalCoreR * 0.5, cx, cy, finalCoreR * 2.2);
    glowGrad.addColorStop(0, `rgba(59, 130, 246, ${0.06 + amp * 0.08})`);
    glowGrad.addColorStop(0.5, `rgba(99, 102, 241, ${0.03 + amp * 0.04})`);
    glowGrad.addColorStop(1, "rgba(99, 102, 241, 0)");
    ctx.beginPath();
    ctx.arc(cx, cy, finalCoreR * 2.2, 0, Math.PI * 2);
    ctx.fillStyle = glowGrad;
    ctx.fill();

    // core fill
    const coreGrad = ctx.createRadialGradient(cx, cy, 0, cx, cy, finalCoreR);
    coreGrad.addColorStop(0, `rgba(219, 234, 254, ${0.7 + amp * 0.2})`);
    coreGrad.addColorStop(0.6, `rgba(191, 219, 254, ${0.45 + amp * 0.15})`);
    coreGrad.addColorStop(1, `rgba(147, 197, 253, ${0.15 + amp * 0.1})`);
    ctx.beginPath();
    ctx.arc(cx, cy, finalCoreR, 0, Math.PI * 2);
    ctx.fillStyle = coreGrad;
    ctx.fill();

    // subtle core ring
    ctx.beginPath();
    ctx.arc(cx, cy, finalCoreR, 0, Math.PI * 2);
    ctx.strokeStyle = `rgba(147, 197, 253, ${0.2 + amp * 0.15})`;
    ctx.lineWidth = 1;
    ctx.stroke();

    // ── 2. Particles ──────────────────────────────────
    const t = time.current;
    for (const p of particles.current) {
      // move
      p.angle += p.speed + amp * p.speed * 3;

      // radial displacement based on amplitude
      const wave = Math.sin(t * BREATH_SPEED * 2 + p.phaseOffset);
      const ampDisplace = amp * MAX_DISPLACEMENT * (0.5 + 0.5 * wave);
      const baseR = half * p.radius * breathScale;
      const r = baseR + ampDisplace;

      // global rotation
      const globalAngle = t * ROTATION_SPEED;
      const angle = p.angle + globalAngle;

      const x = cx + Math.cos(angle) * r;
      const y = cy + Math.sin(angle) * r;

      // pulse opacity
      const opMod = 0.7 + 0.3 * Math.sin(t * 0.008 + p.phaseOffset);
      const finalOp = p.opacity * opMod * (0.5 + amp * 0.5 + (isActive ? 0.2 : 0));

      ctx.beginPath();
      ctx.arc(x, y, p.size * (1 + amp * 0.6), 0, Math.PI * 2);
      ctx.fillStyle = `rgba(${p.color[0]}, ${p.color[1]}, ${p.color[2]}, ${finalOp})`;
      ctx.fill();
    }

    // ── 3. Faint concentric rings ─────────────────────
    for (let i = 1; i <= 3; i++) {
      const ringR = half * (CORE_RADIUS + 0.12 * i) * breathScale + amp * 10 * i;
      ctx.beginPath();
      ctx.arc(cx, cy, ringR, 0, Math.PI * 2);
      ctx.strokeStyle = `rgba(147, 197, 253, ${0.06 - i * 0.012 + amp * 0.04})`;
      ctx.lineWidth = 0.8;
      ctx.stroke();
    }

    frameId.current = requestAnimationFrame(draw);
  }, [amplitude, isActive]);

  /* ---- lifecycle ---- */
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const dpr = window.devicePixelRatio || 1;
    canvas.width  = size * dpr;
    canvas.height = size * dpr;
    const ctx = canvas.getContext("2d")!;
    ctx.scale(dpr, dpr);

    frameId.current = requestAnimationFrame(draw);
    return () => cancelAnimationFrame(frameId.current);
  }, [draw, size]);

  return (
    <canvas
      ref={canvasRef}
      className="voice-canvas"
      style={{ width: size, height: size }}
    />
  );
}
