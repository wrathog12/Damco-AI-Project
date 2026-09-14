"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Check, Info, LogOut, ShieldCheck, Trash2, TriangleAlert } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { useSession } from "../providers";
import { Button } from "../components/ui/Button";
import { Card, Field, Input, Select, Skeleton, Switch } from "../components/ui/primitives";
import type { ProfileFacts, ProfileResponse } from "@/lib/types";
import { cn } from "@/lib/cn";

/** Exactly the vocabulary `services/schemes.py::_ALIASES` canonicalises to.
 *  `gender` and `caste` are exact keyword filters in Qdrant, so case is
 *  load-bearing — a lowercase "female" matches nothing at all. */
const GENDERS = ["Female", "Male", "All"] as const;
const CASTES = ["General", "OBC", "SC", "ST", "EWS"] as const;
const OCCUPATIONS = [
  "Farmer",
  "Student",
  "Worker",
  "Unemployed",
  "Widow",
  "Teacher",
  "Self-employed",
];

const STATES = [
  "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chhattisgarh",
  "Goa", "Gujarat", "Haryana", "Himachal Pradesh", "Jharkhand", "Karnataka",
  "Kerala", "Madhya Pradesh", "Maharashtra", "Manipur", "Meghalaya", "Mizoram",
  "Nagaland", "Odisha", "Punjab", "Rajasthan", "Sikkim", "Tamil Nadu",
  "Telangana", "Tripura", "Uttar Pradesh", "Uttarakhand", "West Bengal",
  "Andaman and Nicobar Islands", "Chandigarh",
  "Dadra and Nagar Haveli and Daman and Diu", "Delhi", "Jammu and Kashmir",
  "Ladakh", "Lakshadweep", "Puducherry",
];

export default function ProfilePage() {
  const router = useRouter();
  const { me, ready, authenticated, refresh, signOut } = useSession();

  const [profile, setProfile] = useState<ProfileResponse | null>(null);
  const [facts, setFacts] = useState<ProfileFacts>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busyAction, setBusyAction] = useState<string | null>(null);

  const consentNeeded = ready && authenticated && me && !me.consent.current;
  const supported = me?.profile.supported ?? true;

  /**
   * Promise callbacks rather than `async`/`await`, and it never raises `loading`
   * itself, because an effect calls it: nothing here may set state in the
   * effect's own tick. A `catch` around an awaited call can be reached
   * synchronously; a `.catch` callback cannot. The initial `true` covers the
   * first load, and every later caller — a save, an erasure — is re-reading
   * behind a form already on screen, where swapping in a skeleton would be worse
   * anyway.
   */
  const load = useCallback(
    () =>
      api
        .profile()
        .then((next) => {
          setProfile(next);
          setFacts(next.facts);
          setError(null);
        })
        .catch((err: unknown) => {
          // 403 `consent_required` is the expected answer before anyone has
          // agreed, not a failure — the consent card below is what resolves it.
          if (err instanceof ApiError && err.code !== "consent_required") {
            setError(err.message);
          }
        })
        .finally(() => setLoading(false)),
    [],
  );

  // `loading` is only ever cleared by `load` itself, which never runs for an
  // anonymous caller — so the not-signed-in branch below is checked *before* the
  // skeleton, rather than this effect clearing a flag that branch does not read.
  useEffect(() => {
    if (ready && authenticated) void load();
  }, [ready, authenticated, load]);

  const save = useCallback(async () => {
    setSaving(true);
    setSaved(false);
    setError(null);
    try {
      await api.saveProfile(facts);
      setSaved(true);
      // The eligible count on `/api/me` is computed from what was just saved.
      await Promise.all([load(), refresh()]);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not save your details.");
    } finally {
      setSaving(false);
    }
  }, [facts, load, refresh]);

  const run = useCallback(
    async (name: string, action: () => Promise<unknown>, after?: () => void) => {
      setBusyAction(name);
      setError(null);
      try {
        await action();
        after?.();
      } catch (err) {
        setError(err instanceof ApiError ? err.message : "That did not work.");
      } finally {
        setBusyAction(null);
      }
    },
    [],
  );

  /* ── Not signed in ──────────────────────────────────────────────────────── */

  if (ready && !authenticated) {
    return (
      <Shell>
        <Card className="p-6">
          <h2 className="text-xl font-bold text-ink">Sign in to save your details</h2>
          <p className="mt-2 text-[1.0625rem] leading-relaxed text-ink-muted">
            Anything personal is only ever stored against an account you can sign
            into and delete. Without one there is nothing to attach it to — so the
            assistant simply asks each time instead.
          </p>
          <Link
            href="/login"
            className="mt-4 inline-flex min-h-12 items-center rounded-xl bg-primary px-5 font-semibold text-on-primary shadow-card hover:bg-primary-hover"
          >
            Sign in with a phone number
          </Link>
        </Card>
      </Shell>
    );
  }

  if (!ready || loading) {
    return (
      <Shell>
        <Card className="space-y-4 p-6">
          <Skeleton className="h-6 w-48" />
          <Skeleton className="h-12 w-full" />
          <Skeleton className="h-12 w-full" />
          <Skeleton className="h-12 w-2/3" />
        </Card>
      </Shell>
    );
  }

  /* ── The server cannot store anything ───────────────────────────────────── */

  if (!supported) {
    return (
      <Shell>
        <Card className="border-warning/35 bg-warning-soft p-6">
          <h2 className="flex items-center gap-2 text-xl font-bold text-warning">
            <Info aria-hidden className="size-5" />
            This server does not store personal details
          </h2>
          <p className="mt-2 text-[1.0625rem] leading-relaxed text-ink">
            No encryption key is configured, so nothing personal can be written at
            all — no details, no transcripts. The assistant still works; it just
            asks your age and state each time instead of remembering them.
          </p>
        </Card>
      </Shell>
    );
  }

  /* ── Consent first ──────────────────────────────────────────────────────── */

  if (consentNeeded) {
    return (
      <Shell>
        <Card className="p-6">
          <h2 className="flex items-center gap-2 text-xl font-bold text-ink">
            <ShieldCheck aria-hidden className="size-5 text-success" />
            Before we store anything
          </h2>
          <div className="mt-3 space-y-3 text-[1.0625rem] leading-relaxed text-ink-muted">
            <p>
              To work out what you qualify for, the assistant needs a few facts:
              your age, state, yearly income, occupation, caste category and
              whether you have a disability or a BPL card.
            </p>
            <p>
              Some of these are sensitive under the DPDP Act, so we ask
              explicitly. They are used for one thing only — matching you to
              schemes — and are encrypted before they are stored.
            </p>
            <p>
              You can change or delete them whenever you like, and deleting your
              account removes everything.
            </p>
          </div>
          <Button
            className="mt-5"
            size="lg"
            loading={busyAction === "consent"}
            onClick={() =>
              void run("consent", api.acceptConsent, () => {
                void refresh();
                void load();
              })
            }
          >
            I agree — store my details
          </Button>
          <p className="mt-3 text-[0.9375rem] text-ink-subtle">
            Notice version {me?.consent.required_version}.{" "}
            <Link href="/privacy" className="font-semibold text-primary hover:underline">
              Read the full detail
            </Link>
          </p>
        </Card>
      </Shell>
    );
  }

  /* ── The form ───────────────────────────────────────────────────────────── */

  const set = <K extends keyof ProfileFacts>(key: K, value: ProfileFacts[K]) => {
    setSaved(false);
    setFacts((prev) => ({ ...prev, [key]: value }));
  };

  /** `""` has to become `null`, not be omitted: the endpoint is a patch-merge,
   *  so an omitted key *keeps* the stored value and clearing a field would
   *  silently do nothing. */
  const text = (value: string) => (value.trim() === "" ? null : value.trim());
  const number = (value: string) => (value.trim() === "" ? null : Number(value));

  return (
    <Shell>
      {error && (
        <p
          role="alert"
          className="mb-4 flex items-start gap-2 rounded-xl border border-danger/30 bg-danger-soft px-4 py-3 text-[0.9375rem] text-danger"
        >
          <TriangleAlert aria-hidden className="mt-0.5 size-[1.15rem] shrink-0" />
          {error}
        </p>
      )}

      <form
        onSubmit={(event) => {
          event.preventDefault();
          void save();
        }}
      >
        <Card className="space-y-5 p-5 sm:p-6">
          <div className="grid gap-5 sm:grid-cols-2">
            <Field label="Age" hint="In years.">
              {(id, describedBy) => (
                <Input
                  id={id}
                  aria-describedby={describedBy}
                  type="number"
                  inputMode="numeric"
                  min={0}
                  max={120}
                  value={facts.age ?? ""}
                  onChange={(event) => set("age", number(event.target.value))}
                />
              )}
            </Field>

            <Field label="Gender">
              {(id) => (
                <Select
                  id={id}
                  value={facts.gender ?? ""}
                  onChange={(event) => set("gender", text(event.target.value))}
                >
                  <option value="">Prefer not to say</option>
                  {GENDERS.map((option) => (
                    <option key={option} value={option}>
                      {option === "All" ? "Other" : option}
                    </option>
                  ))}
                </Select>
              )}
            </Field>

            <Field label="State" hint="Most schemes are state-specific.">
              {(id, describedBy) => (
                <Select
                  id={id}
                  aria-describedby={describedBy}
                  value={facts.state ?? ""}
                  onChange={(event) => set("state", text(event.target.value))}
                >
                  <option value="">Prefer not to say</option>
                  {STATES.map((option) => (
                    <option key={option} value={option}>
                      {option}
                    </option>
                  ))}
                </Select>
              )}
            </Field>

            <Field label="Yearly household income" hint="In rupees, roughly.">
              {(id, describedBy) => (
                <Input
                  id={id}
                  aria-describedby={describedBy}
                  type="number"
                  inputMode="numeric"
                  min={0}
                  step={1000}
                  placeholder="e.g. 180000"
                  value={facts.income ?? ""}
                  onChange={(event) => set("income", number(event.target.value))}
                />
              )}
            </Field>

            <Field label="Occupation">
              {(id) => (
                <>
                  <Input
                    id={id}
                    list="occupations"
                    value={facts.occupation ?? ""}
                    onChange={(event) => set("occupation", text(event.target.value))}
                    placeholder="Farmer, student, shopkeeper…"
                  />
                  {/* A datalist rather than a select: occupation is matched by
                      full-text search server-side, so an unlisted answer still
                      works and forcing a closed list would lose it. */}
                  <datalist id="occupations">
                    {OCCUPATIONS.map((option) => (
                      <option key={option} value={option} />
                    ))}
                  </datalist>
                </>
              )}
            </Field>

            <Field label="Caste category" hint="Only used for reserved schemes.">
              {(id, describedBy) => (
                <Select
                  id={id}
                  aria-describedby={describedBy}
                  value={facts.caste ?? ""}
                  onChange={(event) => set("caste", text(event.target.value))}
                >
                  <option value="">Prefer not to say</option>
                  {CASTES.map((option) => (
                    <option key={option} value={option}>
                      {option}
                    </option>
                  ))}
                </Select>
              )}
            </Field>
          </div>

          <div className="space-y-1 border-t border-line pt-4">
            <Switch
              label="I have a disability"
              checked={facts.disability === true}
              onChange={(next) => set("disability", next ? true : null)}
            />
            <Switch
              label="I have a BPL ration card"
              checked={facts.bpl_card === true}
              onChange={(next) => set("bpl_card", next ? true : null)}
            />
          </div>

          <div className="flex flex-wrap items-center gap-3 border-t border-line pt-4">
            <Button type="submit" size="lg" loading={saving}>
              Save my details
            </Button>
            {saved && (
              <span
                aria-live="polite"
                className="inline-flex items-center gap-1.5 font-semibold text-success"
              >
                <Check aria-hidden className="size-5" />
                Saved
              </span>
            )}
            {profile?.updated_at && !saved && (
              <span className="text-[0.9375rem] text-ink-subtle">
                Last updated {new Date(profile.updated_at).toLocaleDateString("en-IN")}
              </span>
            )}
          </div>

          {me?.profile.eligible_count != null && (
            <Link
              href="/schemes"
              className={cn(
                "flex items-center justify-between gap-3 rounded-xl border border-accent/35",
                "bg-accent-soft px-4 py-3.5 font-semibold text-on-accent hover:bg-accent/25",
              )}
            >
              <span>
                You qualify for {me.profile.eligible_count.toLocaleString("en-IN")}{" "}
                {me.profile.eligible_count === 1 ? "scheme" : "schemes"}
              </span>
              <span aria-hidden>→</span>
            </Link>
          )}
        </Card>
      </form>

      {/* ── Erasure ────────────────────────────────────────────────────────── */}
      <section className="mt-8">
        <h2 className="text-[0.8125rem] font-bold tracking-[0.06em] text-ink-subtle uppercase">
          Your data
        </h2>
        <p className="mt-1.5 mb-3 text-[0.9375rem] leading-relaxed text-ink-muted">
          Three separate things, so you can drop one without losing the others.
          Transcripts are kept for 90 days and then deleted automatically.
        </p>

        <Card className="divide-y divide-line">
          <Erasure
            title="Forget my details"
            body="Removes your age, state, income and the rest. Your account and history stay."
            action="Delete details"
            busy={busyAction === "profile"}
            onConfirm={() =>
              void run("profile", api.deleteProfile, () => {
                setFacts({});
                void load();
                void refresh();
              })
            }
          />
          <Erasure
            title="Forget what I said"
            body="Deletes every stored conversation and the schemes you looked at. Your account and details stay."
            action="Delete history"
            busy={busyAction === "history"}
            onConfirm={() => void run("history", api.deleteHistory)}
          />
          <Erasure
            title="Delete my account"
            body="Removes everything, including your phone number. This cannot be undone."
            action="Delete everything"
            danger
            busy={busyAction === "account"}
            onConfirm={() =>
              void run("account", api.deleteAccount, () => {
                void signOut();
                router.push("/");
              })
            }
          />
        </Card>

        <Button
          variant="ghost"
          className="mt-4"
          onClick={() => void signOut().then(() => router.push("/"))}
        >
          <LogOut aria-hidden className="size-[1.15rem]" />
          Sign out
        </Button>
      </section>
    </Shell>
  );
}

function Shell({ children }: { children: React.ReactNode }) {
  const { me } = useSession();
  return (
    <div className="mx-auto max-w-2xl px-4 pt-8 pb-20 sm:px-6">
      <h1 className="text-[2rem] leading-tight font-extrabold tracking-[-0.03em] text-ink">
        My details
      </h1>
      <p className="mt-2 mb-6 text-[1.0625rem] leading-relaxed text-ink-muted">
        {me?.phone
          ? `Signed in as ${me.phone}. `
          : ""}
        The assistant uses these to work out what you qualify for, so it stops
        asking every time.
      </p>
      {children}
    </div>
  );
}

/**
 * A destructive action behind one deliberate confirm.
 *
 * Inline rather than a modal: a dialog on a low-end Android is a layout risk and
 * a focus-trap to get right, and the two-tap inline pattern gives the same
 * protection — you cannot delete anything with a single mis-tap.
 */
function Erasure({
  title,
  body,
  action,
  danger = false,
  busy,
  onConfirm,
}: {
  title: string;
  body: string;
  action: string;
  danger?: boolean;
  busy: boolean;
  onConfirm: () => void;
}) {
  const [armed, setArmed] = useState(false);

  return (
    <div className="flex flex-col gap-3 p-5 sm:flex-row sm:items-center sm:justify-between">
      <div className="min-w-0">
        <p className={cn("font-bold", danger ? "text-danger" : "text-ink")}>{title}</p>
        <p className="mt-0.5 text-[0.9375rem] leading-relaxed text-ink-muted">{body}</p>
      </div>
      {armed ? (
        <div className="flex shrink-0 items-center gap-2">
          <Button variant="danger" onClick={onConfirm} loading={busy}>
            {busy ? "Deleting" : "Yes, delete"}
          </Button>
          <Button variant="ghost" onClick={() => setArmed(false)} disabled={busy}>
            Cancel
          </Button>
        </div>
      ) : (
        <Button
          variant={danger ? "danger" : "secondary"}
          className="shrink-0"
          onClick={() => setArmed(true)}
        >
          <Trash2 aria-hidden className="size-[1.15rem]" />
          {action}
        </Button>
      )}
    </div>
  );
}
