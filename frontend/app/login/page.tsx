"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { ArrowLeft, Check, ShieldCheck } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { useSession } from "../providers";
import { Button } from "../components/ui/Button";
import { Card, Field, Input } from "../components/ui/primitives";
import type { OtpSent } from "@/lib/types";
import { cn } from "@/lib/cn";

/**
 * Login is an *upgrade*, and the page has to read that way.
 *
 * There is no login wall in this product — a first-time caller talks to the
 * agent immediately, and this screen exists for someone who wants more turns or
 * wants the assistant to remember their details. So the copy leads with what you
 * get, the back link is prominent, and nothing here is framed as a requirement.
 */
export default function LoginPage() {
  const router = useRouter();
  const { signIn, authenticated, ready } = useSession();

  const [step, setStep] = useState<"phone" | "code">("phone");
  const [phone, setPhone] = useState("");
  const [code, setCode] = useState("");
  const [sent, setSent] = useState<OtpSent | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [cooldown, setCooldown] = useState(0);

  // Already signed in? There is nothing to do here.
  useEffect(() => {
    if (ready && authenticated) router.replace("/profile");
  }, [ready, authenticated, router]);

  useEffect(() => {
    if (cooldown <= 0) return;
    const timer = window.setTimeout(() => setCooldown((n) => n - 1), 1000);
    return () => window.clearTimeout(timer);
  }, [cooldown]);

  const request = useCallback(async () => {
    setError(null);
    setBusy(true);
    try {
      const result = await api.requestOtp(phone.trim());
      setSent(result);
      setStep("code");
      setCooldown(result.retry_after);
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.message
          : "Could not send the code. Check the number and try again.",
      );
    } finally {
      setBusy(false);
    }
  }, [phone]);

  const verify = useCallback(async () => {
    setError(null);
    setBusy(true);
    try {
      const session = await api.verifyOtp(phone.trim(), code.trim());
      await signIn(session.access_token);
      router.push("/profile");
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "That code did not work. Try again.",
      );
    } finally {
      setBusy(false);
    }
  }, [phone, code, signIn, router]);

  return (
    <div className="mx-auto max-w-lg px-4 pt-8 pb-20 sm:px-6">
      <Link
        href="/"
        className="mb-6 inline-flex min-h-11 items-center gap-1.5 font-semibold text-ink-muted hover:text-ink"
      >
        <ArrowLeft aria-hidden className="size-[1.15rem]" />
        Back to the assistant
      </Link>

      <h1 className="text-[2rem] leading-tight font-extrabold tracking-[-0.03em] text-ink">
        Sign in with your phone
      </h1>
      <p className="mt-2 text-[1.0625rem] leading-relaxed text-ink-muted">
        You get a much larger daily allowance, and the assistant can remember your
        details so it stops asking the same questions every time.
      </p>

      <ul className="mt-5 space-y-2">
        {[
          "Your turns so far carry over — nothing is lost",
          "No password to remember",
          "You can delete everything at any time",
        ].map((line) => (
          <li key={line} className="flex items-start gap-2.5 text-[0.9375rem] text-ink-muted">
            <Check aria-hidden className="mt-0.5 size-[1.15rem] shrink-0 text-success" />
            {line}
          </li>
        ))}
      </ul>

      <Card className="mt-7 p-5 sm:p-6">
        {step === "phone" ? (
          <form
            onSubmit={(event) => {
              event.preventDefault();
              void request();
            }}
            className="space-y-4"
          >
            <Field
              label="Mobile number"
              hint="An Indian mobile number, 10 digits. We only use it to send your code."
              error={error}
            >
              {(id, describedBy) => (
                <Input
                  id={id}
                  aria-describedby={describedBy}
                  // `tel` rather than `number`: a number input strips leading
                  // zeros, rejects a `+91` prefix and shows spinner arrows.
                  type="tel"
                  inputMode="numeric"
                  autoComplete="tel"
                  autoFocus
                  placeholder="98765 43210"
                  value={phone}
                  onChange={(event) => setPhone(event.target.value)}
                />
              )}
            </Field>
            <Button type="submit" size="lg" block loading={busy} disabled={phone.trim().length < 10}>
              Send me a code
            </Button>
          </form>
        ) : (
          <form
            onSubmit={(event) => {
              event.preventDefault();
              void verify();
            }}
            className="space-y-4"
          >
            <p className="text-[0.9375rem] text-ink-muted">
              Code sent to <span className="font-semibold text-ink">{sent?.phone}</span>.{" "}
              <button
                type="button"
                onClick={() => {
                  setStep("phone");
                  setCode("");
                  setError(null);
                }}
                className="font-semibold text-primary hover:underline"
              >
                Wrong number?
              </button>
            </p>

            <Field label="The 6-digit code" error={error}>
              {(id, describedBy) => (
                <Input
                  id={id}
                  aria-describedby={describedBy}
                  type="text"
                  inputMode="numeric"
                  autoComplete="one-time-code"
                  autoFocus
                  maxLength={6}
                  placeholder="••••••"
                  value={code}
                  onChange={(event) => setCode(event.target.value.replace(/\D/g, ""))}
                  className="text-center text-2xl font-bold tracking-[0.4em]"
                />
              )}
            </Field>

            {/* Only ever present when the server runs the stub provider with
                `OTP_DEV_ECHO=1`. It cannot appear against a real provider, so
                showing it is safe and saves digging through the server log. */}
            {sent?.dev_code && (
              <p
                className={cn(
                  "rounded-xl border border-warning/35 bg-warning-soft px-4 py-3",
                  "text-[0.9375rem] text-warning",
                )}
              >
                Development server: your code is{" "}
                <span className="font-bold tracking-widest">{sent.dev_code}</span>
              </p>
            )}

            <Button type="submit" size="lg" block loading={busy} disabled={code.length < 4}>
              Sign in
            </Button>

            <Button
              type="button"
              variant="ghost"
              block
              disabled={cooldown > 0 || busy}
              onClick={() => void request()}
            >
              {cooldown > 0 ? `Send again in ${cooldown}s` : "Send the code again"}
            </Button>
          </form>
        )}
      </Card>

      <p className="mt-5 flex items-start gap-2 text-[0.9375rem] leading-relaxed text-ink-subtle">
        <ShieldCheck aria-hidden className="mt-0.5 size-[1.15rem] shrink-0 text-success" />
        Your number is stored on its own and never shown back to you in full. See{" "}
        <Link href="/privacy" className="font-semibold text-primary hover:underline">
          what we keep
        </Link>
        .
      </p>
    </div>
  );
}
