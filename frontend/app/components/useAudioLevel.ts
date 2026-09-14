"use client";

import { useEffect, useRef } from "react";

/**
 * A live loudness reading for one audio track, exposed as a **ref**.
 *
 * The ref is the whole point. v1 put the analyser's output in `useState`, which
 * re-rendered the page ~60 times a second; the orb's `draw` callback then listed
 * `amplitude` in its dependencies and the mount effect depended on `draw`, so
 * every one of those renders tore down the requestAnimationFrame loop, resized
 * the canvas and started again. The visualiser was the most expensive component
 * on the page and it was fighting itself.
 *
 * A ref has no such coupling: the animation loop reads `ref.current` whenever it
 * happens to paint, and React never learns that the number changed. Nothing
 * outside a canvas needs to re-render when someone's voice gets louder.
 *
 * `track` may be null (before a call, or while the bot has not spoken yet), and
 * that is the normal state rather than an error — the hook simply idles.
 */
export function useAudioLevel(track: MediaStreamTrack | null | undefined) {
  const level = useRef(0);

  useEffect(() => {
    if (!track) {
      level.current = 0;
      return;
    }

    // One AudioContext per track, closed on teardown. Browsers cap the number of
    // live contexts, and a leaked one keeps the mic indicator on after hang-up.
    const ctx = new AudioContext();
    const source = ctx.createMediaStreamSource(new MediaStream([track]));
    const analyser = ctx.createAnalyser();
    // 512 bins is enough to tell speech from silence and cheap enough to run on
    // a low-end phone alongside a WebRTC call. The FFT is not being used for
    // spectral content — only for a magnitude.
    analyser.fftSize = 512;
    analyser.smoothingTimeConstant = 0.75;
    source.connect(analyser);

    const bins = new Uint8Array(analyser.frequencyBinCount);
    let raf = 0;

    const sample = () => {
      analyser.getByteFrequencyData(bins);
      // Only the low two-thirds of the spectrum: the top bins are hiss and room
      // noise, and including them makes a quiet room read as 15% loud, so the
      // orb never settles.
      const cutoff = Math.floor(bins.length * 0.66);
      let sum = 0;
      for (let i = 0; i < cutoff; i++) sum += bins[i];
      const mean = sum / cutoff / 255;
      // Expanded, then clamped. Speech through a phone mic rarely exceeds ~0.35
      // on this measure, so the raw value would only ever drive a third of the
      // orb's range.
      level.current = Math.min(1, mean * 2.6);
      raf = requestAnimationFrame(sample);
    };
    sample();

    return () => {
      cancelAnimationFrame(raf);
      source.disconnect();
      // `close()` returns a promise; a rejection here means the context was
      // already closed, which is not something a caller can act on.
      void ctx.close().catch(() => {});
      level.current = 0;
    };
  }, [track]);

  return level;
}
