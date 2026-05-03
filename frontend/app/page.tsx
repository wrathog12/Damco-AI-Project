"use client";

import { useState, useRef, useEffect, useCallback } from "react";
import VoiceOrb from "./components/VoiceOrb";
import SchemeCard from "./components/SchemeCard";

type Status = "standby" | "listening" | "processing" | "speaking";

const STATUS_LABELS: Record<Status, string> = {
  standby:    "Ready",
  listening:  "Listening",
  processing: "Processing",
  speaking:   "Speaking",
};

const STATUS_SUBTITLES: Record<Status, string> = {
  standby:    "Tap to start a conversation",
  listening:  "I'm listening… go ahead",
  processing: "Understanding your request…",
  speaking:   "Here's what I found…",
};

export default function Home() {
  const [status, setStatus]        = useState<Status>("standby");
  const [amplitude, setAmplitude]  = useState(0);
  const [activeCard, setActiveCard] = useState<any>(null);

  // Mic / analyser refs
  const streamRef    = useRef<MediaStream | null>(null);
  const analyserRef  = useRef<AnalyserNode | null>(null);
  const audioCtxRef  = useRef<AudioContext | null>(null);
  const rafRef       = useRef<number>(0);
  const wsRef        = useRef<WebSocket | null>(null);
  const stopMicRef   = useRef<(() => void) | null>(null);
  const pcRef        = useRef<RTCPeerConnection | null>(null);
  const audioElRef   = useRef<HTMLAudioElement | null>(null);

  /* ── WebSocket for Scheme Cards + Status ───────────── */
  useEffect(() => {
    let reconnectTimer: NodeJS.Timeout | null = null;
    let ws: WebSocket | null = null;
    let isMounted = true;

    const connect = () => {
      if (!isMounted) return;

      ws = new WebSocket("ws://localhost:8000/ws/cards");
      wsRef.current = ws;

      ws.onopen = () => {
        console.log("[WS] Connected");
      };

      ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);

          if (data.type === "status_change" && data.status) {
            console.log("[WS] Status:", data.status);
            setStatus(data.status as Status);
          } else if (data.type === "show_scheme_card" && data.scheme) {
            console.log("[WS] Card:", data.scheme.scheme_name);
            setActiveCard(data.scheme);
          } else if (data.type === "end_call") {
            console.log("[WS] Call ended:", data.reason);
            setTimeout(() => {
              stopMicRef.current?.();
            }, 2000);
          }
        } catch (err) {
          console.error("[WS] Parse error:", err);
        }
      };

      ws.onerror = () => {};

      ws.onclose = () => {
        wsRef.current = null;
        if (isMounted) {
          reconnectTimer = setTimeout(connect, 3000);
        }
      };
    };

    connect();

    return () => {
      isMounted = false;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      if (ws) ws.close();
    };
  }, []);

  const handleCloseCard = () => {
    setActiveCard(null);
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: "dismiss_card" }));
    }
  };

  /* ── Start microphone & WebRTC ────────────────────── */
  const startMic = useCallback(async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      streamRef.current = stream;

      // Local visualizer
      const ctx = new AudioContext();
      audioCtxRef.current = ctx;
      const source = ctx.createMediaStreamSource(stream);
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 256;
      source.connect(analyser);
      analyserRef.current = analyser;

      // RTCPeerConnection for FastRTC
      const pc = new RTCPeerConnection({
        iceServers: [{ urls: "stun:stun.l.google.com:19302" }]
      });
      pcRef.current = pc;

      pc.createDataChannel("data");
      stream.getTracks().forEach((track) => pc.addTrack(track, stream));

      // Handle incoming audio from backend (TTS)
      pc.ontrack = (event) => {
        if (!audioElRef.current) {
          const audio = new Audio();
          audio.autoplay = true;
          audioElRef.current = audio;
        }
        audioElRef.current.srcObject = event.streams[0];
      };

      // Create and set local description
      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);

      // Wait for ICE gathering
      await new Promise<void>((resolve) => {
        if (pc.iceGatheringState === "complete") {
          resolve();
          return;
        }

        const timeout = setTimeout(() => {
          resolve();
        }, 1500);

        pc.onicegatheringstatechange = () => {
          if (pc.iceGatheringState === "complete") {
            clearTimeout(timeout);
            resolve();
          }
        };
      });

      // Send offer to backend
      const response = await fetch("http://localhost:8000/rtc/webrtc/offer", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          sdp: pc.localDescription?.sdp,
          type: pc.localDescription?.type,
          webrtc_id: Math.random().toString(36).substring(7)
        }),
      });

      const answer = await response.json();
      await pc.setRemoteDescription(answer);

      setStatus("listening");

      // Visualizer polling
      const dataArr = new Uint8Array(analyser.frequencyBinCount);
      const poll = () => {
        analyser.getByteFrequencyData(dataArr);
        let sum = 0;
        for (let i = 0; i < dataArr.length; i++) sum += dataArr[i];
        const avg = sum / dataArr.length / 255;
        setAmplitude(avg);
        rafRef.current = requestAnimationFrame(poll);
      };
      poll();
    } catch (err) {
      console.error("WebRTC Error:", err);
      setStatus("standby");
    }
  }, []);

  /* ── Stop microphone & WebRTC ─────────────────────── */
  const stopMic = useCallback(() => {
    cancelAnimationFrame(rafRef.current);
    streamRef.current?.getTracks().forEach((t) => t.stop());
    pcRef.current?.close();
    audioCtxRef.current?.close();

    if (audioElRef.current) {
      audioElRef.current.pause();
      audioElRef.current.srcObject = null;
    }

    analyserRef.current = null;
    streamRef.current = null;
    audioCtxRef.current = null;
    pcRef.current = null;

    setAmplitude(0);
    setStatus("standby");
  }, []);

  useEffect(() => { stopMicRef.current = stopMic; }, [stopMic]);
  useEffect(() => () => stopMic(), [stopMic]);

  const isActive = status !== "standby";

  return (
    <div className="flex flex-col min-h-screen">
      {/* ── Header ───────────────────────────────────── */}
      <header className="header">
        <div className="max-w-6xl mx-auto flex items-center justify-between px-6 py-3">
          {/* Logo */}
          <div className="flex items-center gap-2.5">
            <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-blue-500 to-indigo-600 flex items-center justify-center shadow-md">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                <path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/>
                <path d="M19 10v2a7 7 0 0 1-14 0v-2"/>
                <line x1="12" x2="12" y1="19" y2="22"/>
              </svg>
            </div>
            <span className="text-xl font-bold tracking-tight" style={{ color: "#1e40af" }}>
              Bhasha<span className="font-medium" style={{ color: "#6366f1" }}>Agent</span>
            </span>
          </div>

          {/* Bot badge */}
          <div
            className="hidden sm:flex items-center gap-2 px-4 py-2 rounded-xl text-sm font-bold text-white shadow-lg"
            style={{
              background: "linear-gradient(135deg, #3b82f6, #6366f1)",
              boxShadow: "0 4px 20px rgba(59,130,246,0.35)",
            }}
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/>
              <path d="M19 10v2a7 7 0 0 1-14 0v-2"/>
            </svg>
            Voice AI
          </div>
        </div>
      </header>

      {/* ── Main Content ────────────────────────────── */}
      <main className="flex-1 flex flex-col items-center justify-center pt-24 pb-10 px-4">
        {/* Title */}
        <div className="text-center mb-8 mt-4">
          <h1 className="text-3xl font-bold tracking-tight mb-2" style={{ color: "#1e3a5f" }}>
            Bhasha-Agent Voice Assistant
          </h1>
          <p className="text-lg mt-1.5" style={{ color: "#64748b" }}>
            Discover government schemes through voice — in your language
          </p>
        </div>

        {/* ── Voice Orb ───────────────────────────────── */}
        <div className="relative mb-6">
          <VoiceOrb
            amplitude={amplitude}
            isActive={isActive}
            status={status}
            size={380}
          />
        </div>

        {/* Status */}
        <div className="mt-8 mb-16 flex flex-col items-center">
          <span className={`status-chip ${status}`}>
            <span className={`status-dot ${status}`} />
            {STATUS_LABELS[status]}
          </span>
          <p className="subtitle-text mt-5" style={{ color: "#64748b" }}>
            {STATUS_SUBTITLES[status]}
          </p>
        </div>

        {/* ── Action Buttons ──────────────────────────── */}
        <div className="flex items-center gap-6 mt-8">
          {status === "standby" ? (
            <button className="btn-primary btn-start" onClick={startMic}>
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                <path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/>
                <path d="M19 10v2a7 7 0 0 1-14 0v-2"/>
                <line x1="12" x2="12" y1="19" y2="22"/>
              </svg>
              Start Talking
            </button>
          ) : (
            <button className="btn-primary btn-stop" onClick={stopMic}>
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                <rect x="6" y="6" width="12" height="12" rx="2"/>
              </svg>
              End Session
            </button>
          )}
        </div>
      </main>

      {/* ── Scheme Card Pop-up ──────────────────────── */}
      {activeCard && (
        <SchemeCard scheme={activeCard} onClose={handleCloseCard} />
      )}
    </div>
  );
}
