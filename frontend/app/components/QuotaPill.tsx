"use client";

import Link from "next/link";
import { Infinity as InfinityIcon, MessageCircle } from "lucide-react";
import { useSession } from "../providers";
import { Skeleton } from "./ui/primitives";
import { cn } from "@/lib/cn";

/**
 * How many turns are left.
 *
 * The framing is load-bearing. The product rule is that login raises a ceiling
 * rather than opening a gate, so this must never read as a lock: it shows a
 * count, and only when the count is genuinely low does it offer signing in as
 * the way to get more. An empty allowance still says "sign in for more", not
 * "access denied" — the caller has already been served, and the conversational
 * surface never refuses them a connection.
 */
export default function QuotaPill() {
  const { quota, ready } = useSession();

  if (!ready) return <Skeleton className="h-11 w-24" />;
  if (!quota) return null;

  const { remaining, limit, authenticated } = quota;
  const ratio = limit > 0 ? remaining / limit : 0;
  const empty = remaining <= 0;
  const low = !empty && ratio <= 0.25;

  const body = (
    <>
      <span
        aria-hidden
        className={cn(
          "grid size-6 place-items-center rounded-full",
          empty
            ? "bg-danger/15 text-danger"
            : low
              ? "bg-warning/20 text-warning"
              : "bg-primary/12 text-primary",
        )}
      >
        {authenticated && remaining > 50 ? (
          <InfinityIcon className="size-3.5" />
        ) : (
          <MessageCircle className="size-3.5" />
        )}
      </span>
      <span className="tabular-nums">
        {remaining}
        <span className="text-ink-subtle">/{limit}</span>
      </span>
      <span className="hidden text-ink-subtle sm:inline">turns</span>
    </>
  );

  const shell = cn(
    "inline-flex min-h-11 items-center gap-2 rounded-xl border px-3",
    "text-[0.9375rem] font-semibold",
    empty
      ? "border-danger/35 bg-danger-soft text-danger"
      : low
        ? "border-warning/35 bg-warning-soft text-warning"
        : "border-line bg-surface text-ink",
  );

  // Signed in already: nothing to offer, so it is a plain readout with the
  // reset time as its tooltip.
  if (authenticated) {
    return (
      <span
        className={shell}
        title={
          quota.resets_at
            ? `Your allowance refreshes ${new Date(quota.resets_at).toLocaleString()}`
            : undefined
        }
      >
        {body}
      </span>
    );
  }

  return (
    <Link
      href="/login"
      className={cn(shell, "transition-colors hover:border-primary/50 hover:bg-primary-soft")}
      title="Sign in with your phone number for many more turns"
    >
      {body}
    </Link>
  );
}
