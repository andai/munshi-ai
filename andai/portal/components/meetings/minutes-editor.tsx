"use client";

import { Plus, Quote, Trash2 } from "lucide-react";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { FormAlert } from "@/components/forms/form-alert";
import { offset } from "@/components/meetings/display";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { ApiError, sentence } from "@/lib/api";
import { UNASSIGNED, saveMinutes, type Minutes, type MinutesDraft, type MomSection, type Segment } from "@/lib/meetings";
import { cn } from "@/lib/utils";

// Edit a version of the minutes and save the result as the next version (`PUT /mom`): the summary,
// and each decision, action item (text, owner, due date), topic and risk; remove items, add items.
// Every item cites the transcript lines it rests on, picked from the stored lines (time, speaker,
// text). Save is offered only when every item has text and at least one cited line; andai-core
// checks it again and its refusal is shown here. The page asks before throwing changes away.

type Kind = Exclude<MomSection, "summary" | "attendees">;
type Row = { key: number; text: string; evidence: string[]; owner: string; due: string };
type Draft = { summary: string } & Record<Kind, Row[]>;

const KINDS: readonly Kind[] = ["decisions", "action_items", "topics", "risks"];
const NOUN: Record<Kind, string> = { decisions: "decision", action_items: "action item", topics: "topic", risks: "risk" };
const DEFAULT_HEADING: Record<Kind, string> = { decisions: "Decisions", action_items: "Action items", topics: "Topics", risks: "Risks" };

const FIELD =
  "w-full min-w-0 rounded-lg border border-input bg-transparent px-2.5 py-2 text-base transition-colors outline-none placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 aria-invalid:border-destructive md:text-sm dark:bg-input/30";

function fromMinutes(minutes: Minutes, next: () => number): Draft {
  const row = (text: string, evidence: string[], owner = UNASSIGNED, due = ""): Row => ({ key: next(), text, evidence: [...evidence], owner, due });
  return {
    summary: minutes.summary,
    decisions: minutes.decisions.map((d) => row(d.text, d.evidence)),
    action_items: minutes.actionItems.map((a) => row(a.text, a.evidence, a.owner || UNASSIGNED, a.due ?? "")),
    topics: minutes.topics.map((t) => row(t.title, t.evidence)),
    risks: minutes.risks.map((r) => row(r.text, r.evidence)),
  };
}

function toDraft(draft: Draft, baseVersion: number): MinutesDraft {
  return {
    baseVersion,
    summary: draft.summary.trim(),
    decisions: draft.decisions.map((r) => ({ text: r.text.trim(), evidence: r.evidence })),
    actionItems: draft.action_items.map((r) => ({ text: r.text.trim(), owner: r.owner, due: r.due || null, evidence: r.evidence })),
    topics: draft.topics.map((r) => ({ title: r.text.trim(), evidence: r.evidence })),
    risks: draft.risks.map((r) => ({ text: r.text.trim(), evidence: r.evidence })),
  };
}

/** What a draft says, without the editor's row keys (for "has anything changed"). */
function content(draft: Draft): string {
  return JSON.stringify(toDraft(draft, 0));
}

const incomplete = (row: Row) => !row.text.trim() || row.evidence.length === 0;

export function MinutesEditor({
  meetingId,
  minutes,
  segments,
  attendees,
  onCancel,
  onSaved,
  onDirtyChange,
}: {
  meetingId: string;
  /** The version being edited (the latest). */
  minutes: Minutes;
  segments: Segment[];
  /** The meeting's attendee names: the owners offered. */
  attendees: string[];
  onCancel: () => void;
  onSaved: (saved: Minutes) => void;
  onDirtyChange: (dirty: boolean) => void;
}) {
  const counter = useRef(0);
  const next = () => ++counter.current;
  const [initial] = useState(() => fromMinutes(minutes, next));
  const [draft, setDraft] = useState<Draft>(initial);
  const [picking, setPicking] = useState<{ kind: Kind; key: number } | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const byId = useMemo(() => new Map(segments.map((s) => [s.id, s])), [segments]);
  const baseline = useMemo(() => content(initial), [initial]);
  const dirty = content(draft) !== baseline;
  useEffect(() => onDirtyChange(dirty), [dirty, onDirtyChange]);

  const blocked = KINDS.reduce((n, kind) => n + draft[kind].filter(incomplete).length, 0);
  const owners = useMemo(() => {
    const names = new Set(attendees.map((n) => n.trim()).filter(Boolean));
    for (const row of draft.action_items) if (row.owner && row.owner !== UNASSIGNED) names.add(row.owner);
    return [...names];
  }, [attendees, draft.action_items]);

  const update = (kind: Kind, key: number, change: Partial<Row>) =>
    setDraft((d) => ({ ...d, [kind]: d[kind].map((r) => (r.key === key ? { ...r, ...change } : r)) }));
  const remove = (kind: Kind, key: number) => setDraft((d) => ({ ...d, [kind]: d[kind].filter((r) => r.key !== key) }));
  const add = (kind: Kind) => {
    const row: Row = { key: next(), text: "", evidence: [], owner: UNASSIGNED, due: "" };
    setDraft((d) => ({ ...d, [kind]: [...d[kind], row] }));
  };
  const pickingRow = picking ? draft[picking.kind].find((r) => r.key === picking.key) : undefined;

  async function save() {
    if (blocked > 0) return;
    setSaving(true);
    setError(null);
    try {
      const saved = await saveMinutes(meetingId, toDraft(draft, minutes.version));
      onSaved(saved);
    } catch (err) {
      setError(err instanceof ApiError ? sentence(err.detail) : "Could not save the minutes. Try again.");
    } finally {
      setSaving(false);
    }
  }

  const kinds = minutes.sections.filter((s): s is Kind => (KINDS as readonly string[]).includes(s));
  for (const kind of KINDS) if (!kinds.includes(kind)) kinds.push(kind);

  return (
    <form
      className="grid gap-1"
      data-testid="minutes-editor"
      onSubmit={(event) => {
        event.preventDefault();
        void save();
      }}
    >
      <EditSection title={minutes.headings.summary ?? "Summary"}>
        <Label htmlFor="edit-summary" className="sr-only">
          Summary
        </Label>
        <textarea id="edit-summary" rows={4} className={FIELD} value={draft.summary} onChange={(e) => setDraft((d) => ({ ...d, summary: e.target.value }))} />
      </EditSection>

      {kinds.map((kind) => (
        <EditSection key={kind} title={minutes.headings[kind] ?? DEFAULT_HEADING[kind]}>
          {draft[kind].length > 0 && (
            <ol className="grid gap-3">
              {draft[kind].map((row, index) => {
                const label = `${NOUN[kind]} ${index + 1}`;
                const invalid = incomplete(row);
                return (
                  <li
                    key={row.key}
                    data-testid="edit-item"
                    data-section={kind}
                    data-invalid={invalid ? "true" : undefined}
                    className={cn("grid gap-2.5 rounded-xl bg-muted/60 p-3", invalid && "ring-1 ring-destructive/40")}
                  >
                    <div className="flex items-start gap-2">
                      <textarea
                        aria-label={`Text of ${label}`}
                        rows={2}
                        className={cn(FIELD, "bg-card")}
                        value={row.text}
                        aria-invalid={!row.text.trim() || undefined}
                        onChange={(e) => update(kind, row.key, { text: e.target.value })}
                      />
                      <Button type="button" variant="ghost" size="icon-lg" aria-label={`Remove ${label}`} onClick={() => remove(kind, row.key)}>
                        <Trash2 aria-hidden="true" />
                      </Button>
                    </div>
                    {kind === "action_items" && (
                      <div className="grid gap-2.5 sm:grid-cols-2">
                        <div className="grid gap-1">
                          <Label htmlFor={`owner-${row.key}`} className="text-xs text-muted-foreground">
                            Owner
                          </Label>
                          <Select value={row.owner} onValueChange={(owner) => update(kind, row.key, { owner })}>
                            <SelectTrigger id={`owner-${row.key}`} aria-label={`Owner of ${label}`} className="h-10 w-full bg-card">
                              <SelectValue />
                            </SelectTrigger>
                            <SelectContent>
                              <SelectItem value={UNASSIGNED}>{minutes.labels.unassigned}</SelectItem>
                              {owners.map((name) => (
                                <SelectItem key={name} value={name}>
                                  {name}
                                </SelectItem>
                              ))}
                            </SelectContent>
                          </Select>
                        </div>
                        <div className="grid gap-1">
                          <Label htmlFor={`due-${row.key}`} className="text-xs text-muted-foreground">
                            Due date
                          </Label>
                          <input
                            id={`due-${row.key}`}
                            type="date"
                            aria-label={`Due date of ${label}`}
                            className={cn(FIELD, "h-10 bg-card py-1")}
                            value={row.due}
                            onChange={(e) => update(kind, row.key, { due: e.target.value })}
                          />
                        </div>
                      </div>
                    )}
                    <div className="flex flex-wrap items-center gap-1.5">
                      {row.evidence.map((id) => (
                        <span
                          key={id}
                          data-testid="edit-evidence"
                          className="inline-flex min-h-7 items-center gap-1 rounded-md border border-border bg-card px-2 font-mono text-xs font-medium text-primary"
                        >
                          <Quote aria-hidden="true" className="size-3" />
                          {offset(byId.get(id)?.start ?? null)}
                        </span>
                      ))}
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        className="bg-card"
                        aria-label={`Cite lines for ${label}`}
                        onClick={() => setPicking({ kind, key: row.key })}
                      >
                        {row.evidence.length ? "Change lines" : "Cite lines"}
                      </Button>
                      {row.evidence.length === 0 && (
                        <span className="text-xs font-medium text-destructive" data-testid="edit-uncited">
                          Cite at least one line of the transcript.
                        </span>
                      )}
                    </div>
                  </li>
                );
              })}
            </ol>
          )}
          <Button type="button" variant="ghost" className="w-fit" onClick={() => add(kind)}>
            <Plus aria-hidden="true" />
            Add {NOUN[kind]}
          </Button>
        </EditSection>
      ))}

      <div className="sticky bottom-0 grid gap-3 border-t border-border bg-card py-4">
        {blocked > 0 && (
          <p className="text-sm text-destructive" data-testid="editor-blocked" role="status">
            {blocked === 1 ? "1 item needs" : `${blocked} items need`} text and at least one cited line before the minutes can be saved.
          </p>
        )}
        {error && <FormAlert>{error}</FormAlert>}
        <div className="flex flex-wrap justify-end gap-2">
          <Button type="button" variant="outline" disabled={saving} onClick={onCancel}>
            Cancel
          </Button>
          <Button type="submit" disabled={saving || blocked > 0 || !dirty} aria-busy={saving}>
            {saving ? "Saving…" : "Save as new version"}
          </Button>
        </div>
      </div>

      <Dialog open={picking !== null && pickingRow !== undefined} onOpenChange={(open) => !open && setPicking(null)}>
        <DialogContent data-testid="evidence-picker" className="grid max-h-[85vh] grid-rows-[auto_minmax(0,1fr)_auto]">
          <DialogHeader>
            <DialogTitle>Cite transcript lines</DialogTitle>
            <DialogDescription>Pick the lines this item rests on. Every item cites at least one.</DialogDescription>
          </DialogHeader>
          <ul className="-mx-1 grid content-start gap-1 overflow-y-auto px-1">
            {segments.map((segment) => {
              const checked = pickingRow?.evidence.includes(segment.id) ?? false;
              const id = `cite-${segment.id}`;
              return (
                <li key={segment.id} data-testid="pick-line" data-segment-id={segment.id}>
                  <div className={cn("flex items-start gap-3 rounded-lg px-2 py-2", checked && "bg-primary/10")}>
                    <Checkbox
                      id={id}
                      className="mt-0.5"
                      checked={checked}
                      onCheckedChange={(on) => {
                        if (!picking || !pickingRow) return;
                        const evidence = on
                          ? segments.filter((s) => s.id === segment.id || pickingRow.evidence.includes(s.id)).map((s) => s.id)
                          : pickingRow.evidence.filter((e) => e !== segment.id);
                        update(picking.kind, picking.key, { evidence });
                      }}
                    />
                    <Label htmlFor={id} className="grid min-w-0 gap-0.5 font-normal">
                      <span className="text-xs text-muted-foreground">
                        <span className="font-mono">{offset(segment.start)}</span> · {segment.speaker}
                      </span>
                      <span className="text-sm [overflow-wrap:anywhere]">{segment.text_raw ?? segment.text}</span>
                    </Label>
                  </div>
                </li>
              );
            })}
          </ul>
          <DialogFooter>
            <Button type="button" onClick={() => setPicking(null)}>
              Done
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </form>
  );
}

function EditSection({ title, children }: { title: string; children: ReactNode }) {
  return (
    <fieldset className="grid gap-3 border-t border-border py-5">
      <legend className="sr-only">{title}</legend>
      <h3 aria-hidden="true" className="text-xs font-bold tracking-wider text-saffron-ink uppercase">
        {title}
      </h3>
      {children}
    </fieldset>
  );
}
