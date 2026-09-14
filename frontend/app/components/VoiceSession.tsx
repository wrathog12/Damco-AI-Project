"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { PipecatClient, RTVIEvent } from "@pipecat-ai/client-js";
import { SmallWebRTCTransport } from "@pipecat-ai/small-webrtc-transport";
import {
  PipecatClientAudio,
  PipecatClientProvider,
  usePipecatClient,
  usePipecatClientMediaTrack,
  usePipecatClientMicControl,
  usePipecatClientTransportState,
  usePipecatConversation,
  useRTVIClientEvent,
} from "@pipecat-ai/client-react";
import type { ConversationMessage, ConversationMessagePart } from "@pipecat-ai/client-react";
import { Mic, MicOff, PhoneOff, TriangleAlert } from "lucide-react";
import VoiceOrb, { type OrbState } from "./VoiceOrb";
import { useAudioLevel } from "./useAudioLevel";
import { Button, IconButton } from "./ui/Button";
import { Card } from "./ui/primitives";
import { getAccessToken } from "@/lib/api";
import { apiUrl } from "@/lib/config";
import { useSession } from "../providers";
import type { Language, ServerEvent, ShowSchemeCardEvent } from "@/lib/types";
import { cn } from "@/lib/cn";

interface VoiceSessionProps {
  /** Called for every card the agent pushes, so the page decides where it goes. */
  onCard: (event: ShowSchemeCardEvent) => void;
  /** The language the caller picked; the agent adapts to speech anyway, but this
   *  is what the card comes back translated into. */
  language: Language;
}

/**
 * Builds the client for one call.
 *
 * **The `Request` object is the whole trick, and it is not stylistic.** The SDK's
 * `makeRequest` constructs its own `Request` with `mode: "cors"` and *no*
 * `credentials`, which under the default `same-origin` policy drops the signed
 * `bh_anon` cookie on the way from `:3000` to `:8000`. Nothing errors: the server
 * simply mints a *fresh* anonymous identity for the offer, so every call gets a
 * new ten-turn allowance and the quota looks like it does not work.
 *
 * The escape hatch is that when `endpoint` is already a `Request` instance, both
 * the offer POST (`negotiate`) and the ICE PATCH (`flushIceCandidates`) reuse it
 * — `new Request(endpoint, { body })` inherits credentials and headers, and the
 * SDK injects the SDP as the body itself. So `credentials: "include"` and the
 * bearer token survive.
 *
 * Corollary: `headers` and `requestData` passed *alongside* a `Request` endpoint
 * are ignored (the SDK logs a warning). Everything has to go on the `Request`.
 */
function buildClient(): PipecatClient {
  const token = getAccessToken();

  const transport = new SmallWebRTCTransport({
    webrtcRequestParams: {
      endpoint: new Request(apiUrl("/api/offer"), {
        method: "POST",
        credentials: "include",
        headers: {
          "Content-Type": "application/json",
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        // Replaced with the SDP by `negotiate()`. A `Request` must be given a
        // body at construction or the method/body pair is fixed as empty.
        body: "{}",
      }),
    },
  });

  return new PipecatClient({
    transport,
    enableMic: true,
    enableCam: false,
    enableScreenShare: false,
  });
}

export default function VoiceSession({ onCard, language }: VoiceSessionProps) {
  const [client, setClient] = useState<PipecatClient | null>(null);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { refresh } = useSession();

  const start = useCallback(async () => {
    setError(null);
    setStarting(true);
    try {
      const next = buildClient();
      // A new call is a new conversation on the server too — one pipeline per
      // `POST /api/offer`, with its own `LLMContext`. Keeping the previous client
      // mounted until this point is what leaves the last call's transcript on
      // screen instead of blanking it the moment someone taps again.
      setClient((previous) => {
        void previous?.disconnect().catch(() => {});
        return next;
      });
      await next.connect();
    } catch (err) {
      setError(
        err instanceof Error && err.message
          ? err.message
          : "Could not start the call. Check your microphone permission and try again.",
      );
    } finally {
      setStarting(false);
      // A spoken turn spends from the same allowance a typed one does, so the
      // header's number is stale from the moment a call begins.
      void refresh();
    }
  }, [refresh]);

  // Hang up if the tab goes away. Without this the server holds a pipeline —
  // and a Deepgram and a Cartesia connection — until its idle timeout fires.
  useEffect(() => {
    if (!client) return;
    return () => {
      void client.disconnect().catch(() => {});
    };
  }, [client]);

  if (!client) {
    return (
      <Idle
        onStart={start}
        starting={starting}
        error={error}
      />
    );
  }

  return (
    <PipecatClientProvider client={client}>
      <Live
        onCard={onCard}
        language={language}
        onRestart={start}
        starting={starting}
        error={error}
      />
      {/* Mounts the bot's audio element. Without it the call connects and the
          agent is inaudible — v1 hand-rolled a detached `new Audio()` for this. */}
      <PipecatClientAudio />
    </PipecatClientProvider>
  );
}

/* ── Before the first call ────────────────────────────────────────────────── */

function Idle({
  onStart,
  starting,
  error,
}: {
  onStart: () => void;
  starting: boolean;
  error: string | null;
}) {
  const quiet = useRef(0);
  return (
    <Shell
      orb={<VoiceOrb level={quiet} state="idle" />}
      status="Tap to start talking"
      hint="Ask in Hindi, English, Bengali, Marathi — or just mix them."
      error={error}
      controls={
        <Button size="lg" loading={starting} onClick={onStart}>
          <Mic aria-hidden className="size-5" />
          Start talking
        </Button>
      }
    />
  );
}

/* ── During and after a call ──────────────────────────────────────────────── */

type Phase = "listening" | "thinking" | "speaking";

function Live({
  onCard,
  language,
  onRestart,
  starting,
  error,
}: {
  onCard: (event: ShowSchemeCardEvent) => void;
  language: Language;
  onRestart: () => void;
  starting: boolean;
  error: string | null;
}) {
  const client = usePipecatClient();
  const transportState = usePipecatClientTransportState();
  const localTrack = usePipecatClientMediaTrack("audio", "local");
  const botTrack = usePipecatClientMediaTrack("audio", "bot");
  const { isMicEnabled, enableMic } = usePipecatClientMicControl();
  const { messages } = usePipecatConversation();

  const [phase, setPhase] = useState<Phase>("listening");
  const [ended, setEnded] = useState<string | null>(null);
  const [liveError, setLiveError] = useState<string | null>(null);

  const localLevel = useAudioLevel(localTrack);
  const botLevel = useAudioLevel(botTrack);

  /* Turn-taking, straight from the VAD events the pipeline already emits. This
     is what the orb's colour means, so it has to track the real turn rather than
     a guess from the transcript. */
  useRTVIClientEvent(
    RTVIEvent.UserStartedSpeaking,
    useCallback(() => setPhase("listening"), []),
  );
  useRTVIClientEvent(
    RTVIEvent.UserStoppedSpeaking,
    useCallback(() => setPhase("thinking"), []),
  );
  useRTVIClientEvent(
    RTVIEvent.BotStartedSpeaking,
    useCallback(() => setPhase("speaking"), []),
  );
  useRTVIClientEvent(
    RTVIEvent.BotStoppedSpeaking,
    useCallback(() => setPhase("listening"), []),
  );

  /* Cards and the hang-up notice arrive as RTVI server messages over the same
     WebRTC connection as the audio. There is no second WebSocket, which is what
     removed v1's flat broadcast list — this handler can only ever see the events
     of the session it is mounted in. */
  useRTVIClientEvent(
    RTVIEvent.ServerMessage,
    useCallback(
      (payload: unknown) => {
        // Empirically `data` *is* the event (see `scripts/two_callers.py`), but
        // the SDK's own type calls it `{data}`, so unwrap one optional level
        // rather than depend on which is true.
        const raw = payload as { type?: string; data?: unknown };
        const event = (typeof raw?.type === "string" ? raw : raw?.data) as
          | ServerEvent
          | undefined;
        if (!event?.type) return;

        if (event.type === "show_scheme_card") {
          onCard({ ...event, language: event.language ?? language });
        } else if (event.type === "end_call") {
          // The pipeline speaks its goodbye first and *then* tears down, so this
          // is a notice, not a teardown to act on.
          setEnded(event.reason ?? "end_call");
        }
      },
      [onCard, language],
    ),
  );

  useRTVIClientEvent(
    RTVIEvent.Error,
    useCallback((payload: unknown) => {
      const message = (payload as { message?: string } | undefined)?.message;
      setLiveError(message || "The connection dropped. Try starting again.");
    }, []),
  );

  const connecting =
    transportState === "initializing" ||
    transportState === "initialized" ||
    transportState === "authenticating" ||
    transportState === "authenticated" ||
    transportState === "connecting";
  const live = transportState === "connected" || transportState === "ready";

  const orbState: OrbState = connecting
    ? "connecting"
    : !live
      ? "idle"
      : !isMicEnabled
        ? "thinking"
        : phase;

  const status = connecting
    ? "Connecting…"
    : !live
      ? ended
        ? "Call ended"
        : "Not connected"
      : !isMicEnabled
        ? "Microphone off"
        : phase === "speaking"
          ? "Speaking"
          : phase === "thinking"
            ? "Looking that up…"
            : "Listening — go ahead";

  const hint = connecting
    ? // The honest number. A cold pipeline spends ~13s opening Deepgram and
      // Cartesia, and silence with no explanation reads as a broken app.
      "Setting up the line. The first connection can take a few seconds."
    : live
      ? "Speak normally. You can interrupt at any time."
      : "Your conversation is below.";

  return (
    <div className="space-y-4">
      <Shell
        orb={
          <VoiceOrb
            level={phase === "speaking" ? botLevel : localLevel}
            state={orbState}
          />
        }
        status={status}
        hint={hint}
        error={liveError ?? error}
        controls={
          live ? (
            <div className="flex items-center gap-2.5">
              <IconButton
                label={isMicEnabled ? "Mute microphone" : "Unmute microphone"}
                variant="secondary"
                size="lg"
                onClick={() => enableMic(!isMicEnabled)}
              >
                {isMicEnabled ? (
                  <Mic aria-hidden className="size-5" />
                ) : (
                  <MicOff aria-hidden className="size-5" />
                )}
              </IconButton>
              <Button
                size="lg"
                variant="danger"
                onClick={() => void client?.disconnect().catch(() => {})}
              >
                <PhoneOff aria-hidden className="size-5" />
                End call
              </Button>
            </div>
          ) : connecting ? (
            <Button size="lg" loading disabled>
              Connecting
            </Button>
          ) : (
            <Button size="lg" loading={starting} onClick={onRestart}>
              <Mic aria-hidden className="size-5" />
              Start again
            </Button>
          )
        }
      />

      <Transcript messages={messages} />
    </div>
  );
}

/* ── Presentation ─────────────────────────────────────────────────────────── */

function Shell({
  orb,
  status,
  hint,
  error,
  controls,
}: {
  orb: React.ReactNode;
  status: string;
  hint: string;
  error: string | null;
  controls: React.ReactNode;
}) {
  return (
    <Card className="relative overflow-hidden px-5 py-8 sm:px-8 sm:py-10">
      {/* `.aurora` is itself `position: absolute; inset: 0`, so it has to be a
          child of the positioned card, not the card. */}
      <div aria-hidden className="aurora" />
      <div className="relative flex flex-col items-center gap-6">
        <div className="size-52 sm:size-64">{orb}</div>

        <div className="space-y-1.5 text-center">
          {/* Polite, not assertive: this changes several times a turn, and an
              assertive region would interrupt the screen reader mid-sentence. */}
          <p aria-live="polite" className="text-xl font-bold tracking-[-0.02em] text-ink">
            {status}
          </p>
          <p className="max-w-xs text-[0.9375rem] leading-relaxed text-ink-muted">
            {hint}
          </p>
        </div>

        {controls}

        {error && (
          <p
            role="alert"
            className={cn(
              "flex max-w-sm items-start gap-2 rounded-xl border border-danger/30",
              "bg-danger-soft px-4 py-3 text-[0.9375rem] text-danger",
            )}
          >
            <TriangleAlert aria-hidden className="mt-0.5 size-[1.15rem] shrink-0" />
            {error}
          </p>
        )}
      </div>
    </Card>
  );
}

/** Assistant parts carry `{spoken, unspoken}`; user parts carry a plain string. */
function partText(part: ConversationMessagePart): string {
  const text = part.text;
  if (typeof text === "string") return text;
  if (text && typeof text === "object" && "spoken" in text) {
    const both = text as { spoken: string; unspoken: string };
    return `${both.spoken}${both.unspoken}`;
  }
  return "";
}

function messageText(message: ConversationMessage): string {
  return message.parts
    .map((part) => (part.needsSeparator ? ` ${partText(part)}` : partText(part)))
    .join("")
    .trim();
}

function Transcript({ messages }: { messages: ConversationMessage[] }) {
  const endRef = useRef<HTMLDivElement>(null);

  // Only the *user* and *assistant* turns. `function_call` rows are real
  // messages in the SDK's model, but "search_schemes({state: 'Bihar'})" on
  // screen tells this audience nothing and makes the panel look like a log.
  const spoken = messages.filter(
    (message) => message.role === "user" || message.role === "assistant",
  );

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "nearest" });
  }, [spoken.length]);

  if (spoken.length === 0) return null;

  return (
    <Card className="scroll-slim max-h-80 overflow-y-auto p-4 sm:p-5">
      <h2 className="mb-3 text-[0.8125rem] font-bold tracking-[0.06em] text-ink-subtle uppercase">
        Conversation
      </h2>
      <ol className="space-y-3">
        {spoken.map((message, i) => {
          const text = messageText(message);
          if (!text) return null;
          const mine = message.role === "user";
          return (
            <li
              key={`${message.createdAt}-${i}`}
              className={cn("flex", mine ? "justify-end" : "justify-start")}
            >
              <p
                className={cn(
                  "max-w-[85%] rounded-2xl px-4 py-2.5 text-[0.9375rem] leading-relaxed",
                  mine
                    ? "bg-primary text-on-primary"
                    : "bg-surface-2 text-ink",
                  // An in-flight turn is dimmed rather than hidden: watching the
                  // words land is how someone knows they were heard at all.
                  message.final === false && "opacity-70",
                )}
              >
                {text}
              </p>
            </li>
          );
        })}
      </ol>
      <div ref={endRef} />
    </Card>
  );
}
