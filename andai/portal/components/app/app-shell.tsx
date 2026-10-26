"use client";

import { LogOut, ShieldCheck } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useCallback, useEffect, useState, type ReactNode } from "react";

import { Wordmark } from "@/components/brand/wordmark";
import { useTheme, type ThemeChoice } from "@/components/theme/theme";
import { Badge } from "@/components/ui/badge";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { api } from "@/lib/api";
import { SessionProvider, displayName, isAdmin, signOut, type Me } from "@/lib/session";
import { cn } from "@/lib/utils";

// The signed-in frame: header (wordmark, navigation, account menu) around every app page. It loads
// the account from andai-core first; a 401 there sends the browser to sign-in or the two-factor
// step (lib/api.ts), so no page renders for a session that is not fully signed in.

type TeamSummary = { id: string; members: { user_id: string; role: "lead" | "member" }[] };

type Loaded = { me: Me; leadsATeam: boolean };

const ROLE_LABEL: Record<Me["role"], string> = { owner: "Owner", admin: "Admin", member: "Member" };

function initials(me: Me): string {
  const source = displayName(me.user);
  const words = source.split(/[\s@._-]+/).filter(Boolean);
  return ((words[0]?.[0] ?? "") + (words[1]?.[0] ?? "")).toUpperCase() || "?";
}

function navFor({ me, leadsATeam }: Loaded): { href: string; label: string }[] {
  const items = [{ href: "/", label: "Meetings" }];
  if (isAdmin(me.role)) {
    items.push(
      { href: "/admin/members", label: "Members" },
      { href: "/admin/teams", label: "Teams" },
      { href: "/admin/security", label: "Security" },
    );
  } else if (leadsATeam) {
    items.push({ href: "/admin/teams", label: "Teams" });
  }
  return items;
}

function NavLinks({ items, className }: { items: { href: string; label: string }[]; className?: string }) {
  const pathname = usePathname();
  return (
    <nav aria-label="Main" className={className}>
      <ul className="flex items-center gap-1">
        {items.map((item) => {
          const active = item.href === "/" ? pathname === "/" : pathname.startsWith(item.href);
          return (
            <li key={item.href}>
              <Link
                href={item.href}
                aria-current={active ? "page" : undefined}
                className={cn(
                  "relative inline-flex h-8 items-center rounded-md px-3 text-sm font-medium whitespace-nowrap text-muted-foreground transition-colors hover:bg-muted hover:text-foreground",
                  active &&
                    "bg-muted text-foreground md:bg-transparent md:after:absolute md:after:inset-x-3 md:after:-bottom-[17px] md:after:h-0.5 md:after:rounded-full md:after:bg-saffron",
                )}
              >
                {item.label}
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}

function AccountMenu({ me }: { me: Me }) {
  const [theme, setTheme] = useTheme();
  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        aria-label="Account menu"
        className="inline-flex size-9 items-center justify-center rounded-full bg-secondary text-xs font-semibold text-secondary-foreground ring-offset-2 ring-offset-background transition hover:ring-2 hover:ring-ring/40 focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
      >
        {initials(me)}
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-64">
        <DropdownMenuLabel className="grid gap-0.5 py-2">
          <span className="truncate text-sm font-medium text-foreground" data-testid="account-name">
            {displayName(me.user)}
          </span>
          {me.user.display_name && (
            <span className="truncate text-xs font-normal text-muted-foreground">{me.user.email}</span>
          )}
          <span className="mt-1 flex items-center gap-1.5 text-xs font-normal text-muted-foreground">
            <span className="truncate">{me.org.name}</span>
            <Badge variant="secondary" data-testid="account-role">
              {ROLE_LABEL[me.role]}
            </Badge>
          </span>
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuGroup>
          <DropdownMenuItem asChild>
            <Link href="/profile/security">
              <ShieldCheck aria-hidden="true" />
              Security settings
            </Link>
          </DropdownMenuItem>
        </DropdownMenuGroup>
        <DropdownMenuSeparator />
        <DropdownMenuLabel className="text-xs font-normal text-muted-foreground">Theme</DropdownMenuLabel>
        <DropdownMenuRadioGroup value={theme} onValueChange={(value) => setTheme(value as ThemeChoice)}>
          <DropdownMenuRadioItem value="system">System</DropdownMenuRadioItem>
          <DropdownMenuRadioItem value="light">Light</DropdownMenuRadioItem>
          <DropdownMenuRadioItem value="dark">Dark</DropdownMenuRadioItem>
        </DropdownMenuRadioGroup>
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={() => void signOut()}>
          <LogOut aria-hidden="true" />
          Sign out
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

export function AppShell({ children }: { children: ReactNode }) {
  const [loaded, setLoaded] = useState<Loaded | null>(null);

  const load = useCallback(async () => {
    const me = await api<Me>("/me");
    let leadsATeam = false;
    try {
      const { teams } = await api<{ teams: TeamSummary[] }>("/teams", { redirect: false });
      leadsATeam = teams.some((team) =>
        team.members.some((member) => member.user_id === me.user.id && member.role === "lead"),
      );
    } catch {
      // the team list only shapes the navigation
    }
    setLoaded({ me, leadsATeam });
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const items = loaded ? navFor(loaded) : [];

  return (
    <div className="flex min-h-dvh flex-col">
      <header className="sticky top-0 z-30 border-b border-border bg-background/85 backdrop-blur supports-[backdrop-filter]:bg-background/70">
        <div className="mx-auto flex h-16 w-full max-w-6xl items-center gap-6 px-4 sm:px-6">
          <Link href="/" className="rounded-md focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none">
            <Wordmark size="sm" />
          </Link>
          {loaded && <NavLinks items={items} className="hidden md:block" />}
          <div className="ml-auto">{loaded && <AccountMenu me={loaded.me} />}</div>
        </div>
        {loaded && items.length > 1 && (
          <NavLinks items={items} className="overflow-x-auto border-t border-border px-2 py-2 md:hidden" />
        )}
      </header>
      {loaded ? (
        <SessionProvider value={{ ...loaded, refresh: load }}>
          <main className="mx-auto w-full max-w-6xl flex-1 px-4 py-8 sm:px-6 sm:py-10">{children}</main>
        </SessionProvider>
      ) : (
        <main className="flex flex-1 items-center justify-center" aria-busy="true">
          <p role="status" className="text-sm text-muted-foreground">
            Loading…
          </p>
        </main>
      )}
    </div>
  );
}
