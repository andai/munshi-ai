import type { ReactNode } from "react";

import { Wordmark } from "@/components/brand/wordmark";
import { ThemeSwitcher } from "@/components/theme/theme";

export default function AuthLayout({ children }: { children: ReactNode }) {
  return (
    <div className="relative flex min-h-dvh flex-col overflow-x-clip">
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-x-0 top-0 -z-10 h-80 bg-[radial-gradient(60%_100%_at_50%_0%,color-mix(in_oklch,var(--ink-bright)_14%,transparent),transparent)]"
      />
      <main className="flex flex-1 flex-col items-center px-4 pt-14 pb-10 sm:pt-20">
        <Wordmark size="lg" />
        <div className="mt-10 w-full max-w-sm">{children}</div>
      </main>
      <footer className="flex items-center justify-between gap-4 px-4 py-4 text-xs text-muted-foreground sm:px-6">
        <span>munshi.ai</span>
        <ThemeSwitcher />
      </footer>
    </div>
  );
}
