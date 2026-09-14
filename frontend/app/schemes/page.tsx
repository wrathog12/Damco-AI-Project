"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { ChevronDown, ListChecks, Search, TriangleAlert } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { useSession } from "../providers";
import { Badge, Card, EmptyState, Skeleton } from "../components/ui/primitives";
import { Button } from "../components/ui/Button";
import type { EligibleResponse } from "@/lib/types";
import { cn } from "@/lib/cn";

/**
 * "You qualify for N schemes" is the payoff for filling in the profile, so this
 * page's job is to make N feel real: every row names the scheme and, when opened,
 * shows the reasons the rules engine actually produced.
 *
 * Those reasons come from `services/eligibility.py`, which is deterministic rules
 * evaluation over the published criteria — never a similarity score. That is the
 * only reason it is defensible to put a verdict in front of someone at all.
 */
export default function SchemesPage() {
  const { ready, authenticated, me } = useSession();
  const [data, setData] = useState<EligibleResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  /**
   * Written with promise callbacks rather than `async`/`await`, and not raising
   * `loading` itself, because it is called from an effect.
   *
   * Both matter for the same reason: nothing here may set state in the effect's
   * own tick, or every mount pays for a cascading re-render. A `catch` on an
   * awaited call *can* run synchronously — a request helper that throws before it
   * ever returns a promise lands there immediately — whereas a `.then`/`.catch`
   * callback is guaranteed to run on a later tick. The initial `true` covers the
   * first load; the one caller that needs a spinner mid-life raises it at the
   * click, where synchronous state is exactly right.
   */
  const load = useCallback(
    () =>
      api
        .eligible(50)
        .then((next) => {
          setData(next);
          setError(null);
        })
        .catch((err: unknown) => {
          if (err instanceof ApiError && err.code === "consent_required") {
            setError(null);
          } else {
            setError(
              err instanceof ApiError ? err.message : "Could not work out your schemes.",
            );
          }
        })
        .finally(() => setLoading(false)),
    [],
  );

  // `loading` starts true and is only ever cleared by `load` itself, so an
  // anonymous caller — for whom `load` never runs — must not be gated on it. The
  // render below asks about `authenticated` first for that reason.
  useEffect(() => {
    if (ready && authenticated) void load();
  }, [ready, authenticated, load]);

  return (
    <div className="mx-auto max-w-3xl px-4 pt-8 pb-20 sm:px-6">
      <h1 className="text-[2rem] leading-tight font-extrabold tracking-[-0.03em] text-ink">
        My schemes
      </h1>
      <p className="mt-2 mb-6 text-[1.0625rem] leading-relaxed text-ink-muted">
        Everything in the corpus, checked against your details one rule at a time.
      </p>

      {!ready ? (
        <Loading />
      ) : !authenticated ? (
        <Card>
          <EmptyState
            icon={<ListChecks aria-hidden className="size-7" />}
            title="Sign in to see your list"
            action={
              <Link
                href="/login"
                className="inline-flex min-h-12 items-center rounded-xl bg-primary px-5 font-semibold text-on-primary shadow-card hover:bg-primary-hover"
              >
                Sign in with a phone number
              </Link>
            }
          >
            This list is worked out from details stored against your account, so
            there has to be an account to store them against.
          </EmptyState>
        </Card>
      ) : loading ? (
        <Loading />
      ) : error ? (
        <p
          role="alert"
          className="flex items-start gap-2 rounded-xl border border-danger/30 bg-danger-soft px-4 py-3 text-[0.9375rem] text-danger"
        >
          <TriangleAlert aria-hidden className="mt-0.5 size-[1.15rem] shrink-0" />
          {error}
        </p>
      ) : !data || data.profile_fields.length === 0 ? (
        <Card>
          <EmptyState
            icon={<ListChecks aria-hidden className="size-7" />}
            title="Nothing to check against yet"
            action={
              <Link
                href="/profile"
                className="inline-flex min-h-12 items-center rounded-xl bg-primary px-5 font-semibold text-on-primary shadow-card hover:bg-primary-hover"
              >
                Add my details
              </Link>
            }
          >
            {/* An empty list here means "nothing stored to evaluate", which is a
                very different thing to tell someone than "you qualify for
                nothing". */}
            Add your age, state and income and this fills in immediately. Two
            minutes of typing, and then the assistant never asks again.
          </EmptyState>
        </Card>
      ) : data.schemes.length === 0 ? (
        <Card>
          <EmptyState
            icon={<Search aria-hidden className="size-7" />}
            title="No matches from the rules alone"
            action={
              <Link
                href="/"
                className="inline-flex min-h-12 items-center rounded-xl bg-primary px-5 font-semibold text-on-primary shadow-card hover:bg-primary-hover"
              >
                Ask the assistant instead
              </Link>
            }
          >
            Nothing in the {data.total.toLocaleString("en-IN")} schemes matched
            every rule against the details you have given. Filling in more of them
            usually helps, and the assistant can search on things the rules do not
            cover.
          </EmptyState>
        </Card>
      ) : (
        <>
          <Card className="mb-5 flex flex-wrap items-center justify-between gap-3 border-accent/35 bg-accent-soft p-5">
            <div>
              <p className="text-2xl font-extrabold text-on-accent">
                {data.eligible_count.toLocaleString("en-IN")}{" "}
                {data.eligible_count === 1 ? "scheme" : "schemes"}
              </p>
              <p className="text-[0.9375rem] text-on-accent/85">
                out of {data.total.toLocaleString("en-IN")}, using{" "}
                {data.profile_fields.length} of your details
              </p>
            </div>
            <Button
              variant="secondary"
              onClick={() => {
                setLoading(true);
                void load();
              }}
            >
              Check again
            </Button>
          </Card>

          <ul className="space-y-3">
            {data.schemes.map((scheme) => (
              <SchemeRow
                key={scheme.scheme_id}
                name={scheme.scheme_name}
                reasons={scheme.reasons}
              />
            ))}
          </ul>

          {data.eligible_count > data.schemes.length && (
            <p className="mt-4 text-[0.9375rem] text-ink-subtle">
              Showing the first {data.schemes.length}. Ask the assistant about a
              category to narrow the rest down.
            </p>
          )}
        </>
      )}

      {me?.profile.has_profile && (
        <p className="mt-8 text-[0.9375rem] text-ink-subtle">
          Based on the details in{" "}
          <Link href="/profile" className="font-semibold text-primary hover:underline">
            My details
          </Link>
          . Always confirm on the official page before applying.
        </p>
      )}
    </div>
  );
}

function Loading() {
  return (
    <div className="space-y-3">
      {[0, 1, 2, 3].map((i) => (
        <Skeleton key={i} className="h-20 w-full" />
      ))}
    </div>
  );
}

function SchemeRow({ name, reasons }: { name: string; reasons: string[] }) {
  const [open, setOpen] = useState(false);

  return (
    <li>
      <Card className="overflow-hidden">
        <button
          type="button"
          aria-expanded={open}
          onClick={() => setOpen((prev) => !prev)}
          className="flex w-full items-center gap-3 px-5 py-4 text-left hover:bg-surface-2"
        >
          <span className="min-w-0 flex-1">
            <span className="block font-bold text-ink">{name}</span>
            <span className="text-[0.875rem] text-ink-subtle">
              {reasons.length} {reasons.length === 1 ? "rule" : "rules"} checked
            </span>
          </span>
          <Badge tone="success">You qualify</Badge>
          <ChevronDown
            aria-hidden
            className={cn(
              "size-5 shrink-0 text-ink-subtle transition-transform",
              open && "rotate-180",
            )}
          />
        </button>
        {open && (
          <ul className="animate-fade space-y-1.5 border-t border-line bg-surface-2/50 px-5 py-4">
            {reasons.map((reason, i) => (
              <li key={i} className="text-[0.9375rem] leading-relaxed text-ink-muted">
                {reason}
              </li>
            ))}
          </ul>
        )}
      </Card>
    </li>
  );
}
