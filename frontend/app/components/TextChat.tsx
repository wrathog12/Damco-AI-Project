"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowUp, TriangleAlert } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import type { ChatTurn, Language, ShowSchemeCardEvent } from "@/lib/types";
import { useSession } from "../providers";
import { Card, Textarea } from "./ui/primitives";
import { IconButton } from "./ui/Button";
import { cn } from "@/lib/cn";

interface TextChatProps {
  onCard: (event: ShowSchemeCardEvent) => void;
  language: Language;
}

interface Bubble {
  role: "user" | "assistant";
  text: string;
}

const SUGGESTIONS = [
  "Scholarships for a student in Bihar",
  "Pension schemes for my grandmother",
  "Loans to start a small shop",
  "क्या मुझे कोई योजना मिल सकती है?",
];

export default function TextChat({ onCard, language }: TextChatProps) {
  const { applyQuota, quota } = useSession();
  const [bubbles, setBubbles] = useState<Bubble[]>([]);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  /** The server's own transcript, echoed back verbatim each turn. It carries
   *  tool calls and their results, so it is not the same list as `bubbles` and
   *  must not be rebuilt from one. */
  const history = useRef<ChatTurn[]>([]);
  /** Generated here so the thread opts into persistence. A bare uuid, because
   *  `conversations.id` is `String(36)` and anything longer is silently dropped
   *  server-side. */
  const conversationId = useRef<string>("");
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!conversationId.current) conversationId.current = crypto.randomUUID();
  }, []);

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "nearest" });
  }, [bubbles.length, sending]);

  const send = useCallback(
    async (text: string) => {
      const message = text.trim();
      if (!message || sending) return;

      setError(null);
      setDraft("");
      setBubbles((prev) => [...prev, { role: "user", text: message }]);
      setSending(true);

      try {
        const res = await api.chat({
          message,
          history: history.current,
          language,
          conversation_id: conversationId.current || undefined,
        });
        history.current = res.history;
        setBubbles((prev) => [...prev, { role: "assistant", text: res.response }]);
        // The response carries the post-turn allowance, so the header updates
        // without a second round trip. A refusal is a 200 whose `response` *is*
        // the refusal — there is deliberately no error path to take here.
        applyQuota(res.quota);
        // Passed straight through: `/chat` returns the same event the voice path
        // sends, so re-wrapping it here would nest the envelope inside itself.
        if (res.card) onCard(res.card);
      } catch (err) {
        setError(
          err instanceof ApiError
            ? err.message
            : "Could not reach the assistant. Check your connection and try again.",
        );
      } finally {
        setSending(false);
      }
    },
    [applyQuota, language, onCard, sending],
  );

  const empty = bubbles.length === 0;

  return (
    <Card className="flex flex-col overflow-hidden">
      <div
        className={cn(
          "scroll-slim overflow-y-auto px-4 py-5 sm:px-6",
          empty ? "" : "max-h-[26rem]",
        )}
      >
        {empty ? (
          <div className="space-y-4 text-center">
            <p className="text-[1.0625rem] leading-relaxed text-ink-muted">
              Type your question in whatever language you are comfortable with.
            </p>
            <ul className="flex flex-wrap justify-center gap-2">
              {SUGGESTIONS.map((suggestion) => (
                <li key={suggestion}>
                  <button
                    type="button"
                    onClick={() => void send(suggestion)}
                    className={cn(
                      "min-h-11 rounded-full border border-line-strong bg-surface-2 px-4",
                      "text-[0.9375rem] font-medium text-ink transition-colors",
                      "hover:border-primary/45 hover:bg-primary-soft hover:text-primary",
                    )}
                  >
                    {suggestion}
                  </button>
                </li>
              ))}
            </ul>
          </div>
        ) : (
          <ol className="space-y-3">
            {bubbles.map((bubble, i) => (
              <li
                key={i}
                className={cn("flex", bubble.role === "user" ? "justify-end" : "justify-start")}
              >
                <p
                  className={cn(
                    "max-w-[85%] rounded-2xl px-4 py-2.5 text-[1rem] leading-relaxed whitespace-pre-wrap",
                    bubble.role === "user"
                      ? "bg-primary text-on-primary"
                      : "bg-surface-2 text-ink",
                  )}
                >
                  {bubble.text}
                </p>
              </li>
            ))}
            {sending && (
              <li className="flex justify-start" aria-live="polite">
                <p className="flex items-center gap-1.5 rounded-2xl bg-surface-2 px-4 py-3">
                  <span className="sr-only">Working on your answer</span>
                  {[0, 1, 2].map((dot) => (
                    <span
                      key={dot}
                      aria-hidden
                      className="animate-breathe size-2 rounded-full bg-ink-subtle"
                      style={{ animationDelay: `${dot * 0.18}s` }}
                    />
                  ))}
                </p>
              </li>
            )}
          </ol>
        )}
        <div ref={endRef} />
      </div>

      {error && (
        <p
          role="alert"
          className="mx-4 mb-2 flex items-start gap-2 rounded-xl border border-danger/30 bg-danger-soft px-4 py-3 text-[0.9375rem] text-danger sm:mx-6"
        >
          <TriangleAlert aria-hidden className="mt-0.5 size-[1.15rem] shrink-0" />
          {error}
        </p>
      )}

      <form
        onSubmit={(event) => {
          event.preventDefault();
          void send(draft);
        }}
        className="flex items-end gap-2 border-t border-line bg-surface-2/50 px-4 py-3 sm:px-6"
      >
        <label htmlFor="chat-input" className="sr-only">
          Your question
        </label>
        <Textarea
          id="chat-input"
          rows={1}
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            // Enter sends, Shift+Enter breaks the line. On a phone the on-screen
            // keyboard's return key inserts a newline instead, which is why the
            // send button is always present rather than being the only path.
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              void send(draft);
            }
          }}
          placeholder="Ask about a scheme…"
          className="max-h-32 min-h-12 flex-1"
        />
        <IconButton
          label="Send"
          type="submit"
          loading={sending}
          disabled={!draft.trim()}
        >
          <ArrowUp aria-hidden className="size-5" />
        </IconButton>
      </form>

      {quota && !quota.allowed && (
        <p className="border-t border-line bg-accent-soft px-4 py-3 text-[0.9375rem] text-on-accent sm:px-6">
          You have used today&apos;s free turns. Signing in with your phone number
          raises the limit to {quota.authenticated ? quota.limit : 100} turns a day.
        </p>
      )}
    </Card>
  );
}
