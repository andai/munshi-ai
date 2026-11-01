"use client";

import { useEffect, useState } from "react";

import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { api } from "@/lib/api";
import type { ListFilter } from "@/lib/meetings";
import { cn } from "@/lib/utils";

// Whose meetings the dashboard lists: "All I can see", "Requested by me", "Shared with me", or one
// team's. andai-core takes one filter at a time, so picking a team replaces the choice of whose
// meetings, and picking whose meetings sets the team back to "All teams". The team list is the
// org's teams (`GET /teams`); a team filter still lists only meetings the caller may open.

type TeamOption = { id: string; name: string };

const SCOPES: { value: "all" | "mine" | "shared"; label: string }[] = [
  { value: "all", label: "All I can see" },
  { value: "mine", label: "Requested by me" },
  { value: "shared", label: "Shared with me" },
];

const ALL_TEAMS = "all";

export function MeetingFilters({ filter, onChange }: { filter: ListFilter; onChange: (filter: ListFilter) => void }) {
  const [teams, setTeams] = useState<TeamOption[]>([]);

  useEffect(() => {
    let active = true;
    api<{ teams: TeamOption[] }>("/teams", { redirect: false })
      .then(({ teams: all }) => {
        if (active) setTeams(all.map(({ id, name }) => ({ id, name })));
      })
      .catch(() => {
        // Without the list there is no team filter; the other filters still work.
      });
    return () => {
      active = false;
    };
  }, []);

  const team = filter.startsWith("team:") ? filter.slice("team:".length) : ALL_TEAMS;

  return (
    <div className="flex flex-wrap items-end gap-3" data-testid="meeting-filters">
      <div role="group" aria-label="Whose meetings" className="grid w-full grid-cols-3 gap-0.5 rounded-xl bg-muted p-1 sm:inline-flex sm:w-auto">
        {SCOPES.map((scope) => {
          const pressed = filter === scope.value;
          return (
            <button
              key={scope.value}
              type="button"
              aria-pressed={pressed}
              onClick={() => onChange(scope.value)}
              className={cn(
                "min-h-9 rounded-lg px-2 py-1 text-[13px] leading-tight font-medium text-muted-foreground transition-colors outline-none focus-visible:ring-3 focus-visible:ring-ring/50 sm:px-3 sm:text-sm",
                pressed ? "bg-card text-foreground shadow-xs" : "hover:text-foreground",
              )}
            >
              {scope.label}
            </button>
          );
        })}
      </div>
      {teams.length > 0 && (
        <div className="grid w-full gap-1 sm:w-auto">
          <span id="team-filter-label" className="text-xs font-medium text-muted-foreground">
            Team
          </span>
          <Select value={team} onValueChange={(value) => onChange(value === ALL_TEAMS ? "all" : `team:${value}`)}>
            <SelectTrigger aria-labelledby="team-filter-label" className="h-10 w-full sm:w-auto sm:min-w-44" data-testid="team-filter">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL_TEAMS}>All teams</SelectItem>
              {teams.map((t) => (
                <SelectItem key={t.id} value={t.id}>
                  {t.name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      )}
    </div>
  );
}
