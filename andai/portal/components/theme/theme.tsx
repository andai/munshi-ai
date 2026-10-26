"use client";

// Light and dark themes. The `dark` class on <html> selects the dark tokens in globals.css. The
// choice is "system" (follow prefers-color-scheme, the default), "light" or "dark"; an explicit
// choice is a per-browser convenience kept in localStorage. ThemeScript (theme-script.tsx) runs before
// first paint so the page never flashes the wrong theme.

import { Monitor, Moon, Sun } from "lucide-react";
import { useEffect, useState } from "react";

import { STORAGE_KEY } from "@/components/theme/theme-script";

export type ThemeChoice = "system" | "light" | "dark";

function readChoice(): ThemeChoice {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored === "light" || stored === "dark") return stored;
  } catch {
    // storage unavailable: follow the system
  }
  return "system";
}

function apply(choice: ThemeChoice): void {
  const dark =
    choice === "dark" ||
    (choice === "system" && window.matchMedia("(prefers-color-scheme: dark)").matches);
  // Switch every colour at once: transitions are paused for the frame that changes the theme.
  const root = document.documentElement;
  root.classList.add("theme-switching");
  root.classList.toggle("dark", dark);
  requestAnimationFrame(() => requestAnimationFrame(() => root.classList.remove("theme-switching")));
}

export function useTheme(): [ThemeChoice, (choice: ThemeChoice) => void] {
  const [choice, setChoice] = useState<ThemeChoice>("system");

  useEffect(() => {
    setChoice(readChoice());
  }, []);

  useEffect(() => {
    if (choice !== "system") return;
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const follow = () => apply("system");
    media.addEventListener("change", follow);
    return () => media.removeEventListener("change", follow);
  }, [choice]);

  const update = (next: ThemeChoice) => {
    try {
      if (next === "system") localStorage.removeItem(STORAGE_KEY);
      else localStorage.setItem(STORAGE_KEY, next);
    } catch {
      // storage unavailable: the choice lasts for this page only
    }
    setChoice(next);
    apply(next);
  };
  return [choice, update];
}

const OPTIONS: { value: ThemeChoice; label: string; Icon: typeof Sun }[] = [
  { value: "system", label: "System", Icon: Monitor },
  { value: "light", label: "Light", Icon: Sun },
  { value: "dark", label: "Dark", Icon: Moon },
];

/** A three-way segmented control: system, light, dark. */
export function ThemeSwitcher({ className }: { className?: string }) {
  const [choice, setChoice] = useTheme();
  return (
    <div
      role="radiogroup"
      aria-label="Theme"
      className={
        "inline-flex items-center gap-0.5 rounded-lg border border-border bg-muted/60 p-0.5 " +
        (className ?? "")
      }
    >
      {OPTIONS.map(({ value, label, Icon }) => (
        <button
          key={value}
          type="button"
          role="radio"
          aria-checked={choice === value}
          aria-label={label}
          title={label}
          onClick={() => setChoice(value)}
          className="inline-flex size-7 items-center justify-center rounded-md text-muted-foreground transition-colors hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none aria-checked:bg-background aria-checked:text-foreground aria-checked:shadow-sm"
        >
          <Icon className="size-3.5" aria-hidden="true" />
        </button>
      ))}
    </div>
  );
}
