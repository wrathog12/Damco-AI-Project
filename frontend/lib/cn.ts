import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

/**
 * Merge class names, last-one-wins on conflicts.
 *
 * `clsx` flattens conditionals; `twMerge` is the part that matters — without it
 * a component's default `px-4` and a caller's `px-6` both land in the class
 * attribute and which one applies depends on the order Tailwind happened to
 * emit them in, not on the order they were written.
 */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}
