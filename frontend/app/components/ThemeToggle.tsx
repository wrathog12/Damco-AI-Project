"use client";

import { Moon, Sun } from "lucide-react";
import { useTheme } from "../providers";
import { cn } from "@/lib/cn";

/**
 * Two states, not three.
 *
 * `system` exists in the provider because it is the right *default* — a phone
 * whose owner set dark mode once should open dark. But a three-way control means
 * explaining "system" to someone who has never heard the concept, so the button
 * simply flips to the opposite of what is currently on screen and writes an
 * explicit choice. Anyone who wants to go back to following the OS clears the
 * site's storage, which is not a workflow worth a third state in the header.
 */
export default function ThemeToggle() {
  const { resolved, setTheme } = useTheme();
  const next = resolved === "dark" ? "light" : "dark";

  return (
    <button
      type="button"
      onClick={() => setTheme(next)}
      aria-label={`Switch to ${next} mode`}
      title={`Switch to ${next} mode`}
      className={cn(
        "grid size-11 place-items-center rounded-xl border border-line-strong",
        "bg-surface text-ink-muted transition-colors hover:bg-surface-2 hover:text-ink",
      )}
    >
      {resolved === "dark" ? (
        <Sun aria-hidden className="size-[1.15rem]" />
      ) : (
        <Moon aria-hidden className="size-[1.15rem]" />
      )}
    </button>
  );
}
