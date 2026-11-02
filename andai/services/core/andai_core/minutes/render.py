"""Minutes as files people download (MOM-04): Markdown, and the HTML the PDF is printed from.

`markdown(doc, context)` and `html(doc, context)` lay out one stored SPEC `MomDocument` (a version's
`body`) the way the meeting page does: the sections in the order of the document's template, each
under the template's heading in the document's output language (`minutes.templates`), "None
recorded" for an empty section, the unassigned label for an action item without an owner, and each
item's evidence as the transcript times (mm:ss from the meeting's start) of the segments it cites.
`context` (`Context`) carries what is not in the document: the meeting's name, date, attendees and
segment start times.

Everything the document or the meeting holds is text written by people or a model, so `html`
escapes every value and `markdown` escapes the characters Markdown would act on; neither emits a
link or an image. The HTML references nothing outside itself (inline styles, fonts by family name
from the system), and `minutes.pdf` refuses every URL anyway.

`filename(context, version, extension)` is an ASCII-only download name.
"""

from __future__ import annotations

import html as _html
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date

from andai_core.minutes import templates

UNASSIGNED = "unassigned"

# The words around the template's headings, per output language.
WORDS: dict[str, dict[str, str]] = {
    "en": {"untitled": "Meeting of {date}", "version": "version {n}", "due": "due"},
    "hi": {"untitled": "{date} की मीटिंग", "version": "संस्करण {n}", "due": "नियत तिथि"},
    "hinglish": {"untitled": "{date} ki meeting", "version": "version {n}", "due": "due"},
}
# The `lang` of the HTML: Hinglish is Hindi in Roman script.
HTML_LANG = {"en": "en", "hi": "hi", "hinglish": "hi-Latn"}


@dataclass(frozen=True)
class Context:
    """What a download shows besides the document."""

    title: str | None
    meeting_date: date | None
    attendees: Sequence[str] = ()
    times: Mapping[str, float] = field(default_factory=dict)  # segment id → start, seconds


@dataclass(frozen=True)
class _Item:
    text: str
    detail: str  # owner and due date of an action item, else ""
    times: list[str]


def clock(seconds: float) -> str:
    """mm:ss from the meeting's start (minutes keep counting past 59)."""
    whole = max(int(seconds), 0)
    return f"{whole // 60:02d}:{whole % 60:02d}"


def _one_line(text: str) -> str:
    return " ".join(str(text or "").split())


def _times(evidence: Sequence[str], context: Context) -> list[str]:
    starts = sorted({context.times[e] for e in evidence if e in context.times})
    return [clock(s) for s in starts]


def name(doc: Mapping, context: Context) -> str:
    """The meeting's title, else "Meeting of <date>" in the document's language."""
    words = WORDS[doc["outputLanguage"]]
    title = _one_line(context.title or "")
    if title:
        return title
    when = context.meeting_date.isoformat() if context.meeting_date else ""
    return words["untitled"].format(date=when)


def _sections(doc: Mapping, context: Context) -> list[tuple[str, str | None, list[_Item]]]:
    """(heading, paragraph, items) per section of the template, in its order."""
    language = doc["outputLanguage"]
    template = templates.get(doc["templateId"])
    words = WORDS[language]
    out: list[tuple[str, str | None, list[_Item]]] = []
    for section in template.sections:
        heading = template.heading(section, language)
        if section == "summary":
            out.append((heading, _one_line(doc.get("summary", "")) or None, []))
            continue
        if section == "attendees":
            people = [_one_line(a) for a in context.attendees if _one_line(a)]
            out.append((heading, None, [_Item(p, "", []) for p in people]))
            continue
        key = {"action_items": "actionItems"}.get(section, section)
        items = []
        for entry in doc.get(key, []):
            text = _one_line(entry.get("title") if section == "topics" else entry.get("text"))
            detail = ""
            if section == "action_items":
                owner = entry.get("owner") or UNASSIGNED
                detail = templates.UNASSIGNED_LABEL[language] if owner == UNASSIGNED else owner
                if entry.get("due"):
                    detail += f", {words['due']} {entry['due']}"
            items.append(_Item(text, _one_line(detail), _times(entry.get("evidence", []), context)))
        out.append((heading, None, items))
    return out


def _subtitle(doc: Mapping, context: Context) -> str:
    language = doc["outputLanguage"]
    parts = [templates.get(doc["templateId"]).name(language)]
    if context.meeting_date:
        parts.append(context.meeting_date.isoformat())
    if doc.get("version"):
        parts.append(WORDS[language]["version"].format(n=doc["version"]))
    return " · ".join(parts)


# ─── Markdown ─────────────────────────────────────────────────────────────────────────────────────

_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]()<>#+!|~])")


def md_escape(text: str) -> str:
    """`text` on one line with every character Markdown acts on escaped."""
    escaped = _MD_SPECIAL.sub(r"\\\1", _one_line(text))
    # A leading "-" or "1." would start a list.
    return re.sub(r"^-", r"\\-", re.sub(r"^(\d+)\.", r"\1\\.", escaped))


def markdown(doc: Mapping, context: Context) -> str:
    """The document as Markdown, laid out by its template in its language."""
    none = templates.NONE_RECORDED[doc["outputLanguage"]]
    lines = [
        f"# {md_escape(name(doc, context))}", "", f"_{md_escape(_subtitle(doc, context))}_", "",
    ]  # fmt: skip
    for heading, paragraph, items in _sections(doc, context):
        lines += [f"## {md_escape(heading)}", ""]
        if paragraph:
            lines.append(md_escape(paragraph))
        elif items:
            for item in items:
                line = f"- {md_escape(item.text)}"
                if item.detail:
                    line += f" — {md_escape(item.detail)}"
                if item.times:
                    line += f" ({', '.join(item.times)})"
                lines.append(line)
        else:
            lines.append(f"_{md_escape(none)}_")
        lines.append("")
    return "\n".join(lines)


# ─── HTML (for the PDF) ───────────────────────────────────────────────────────────────────────────

STYLE = """
@page { size: A4; margin: 18mm 16mm 20mm;
  @bottom-right { content: counter(page) " / " counter(pages); font-size: 8pt; color: #6b7280; } }
html { font-family: "Noto Sans", "Noto Sans Devanagari", sans-serif; font-size: 10.5pt;
  line-height: 1.45; color: #111827; }
h1 { font-size: 18pt; margin: 0 0 2pt; color: #312e81; }
.sub { color: #6b7280; font-size: 9pt; margin: 0 0 14pt; border-bottom: 2pt solid #f59e0b;
  padding-bottom: 6pt; }
h2 { font-size: 12pt; margin: 14pt 0 4pt; color: #312e81; page-break-after: avoid; }
ul { margin: 0; padding-left: 14pt; }
li { margin: 0 0 3pt; page-break-inside: avoid; }
.detail { color: #374151; }
.at { color: #6b7280; font-size: 8.5pt; white-space: nowrap; }
.none { color: #6b7280; font-style: italic; }
"""


def html(doc: Mapping, context: Context) -> str:
    """The document as a standalone, escaped HTML page with print CSS."""
    esc = _html.escape
    language = doc["outputLanguage"]
    none = templates.NONE_RECORDED[language]
    body = [
        f"<h1>{esc(name(doc, context))}</h1>",
        f'<p class="sub">{esc(_subtitle(doc, context))}</p>',
    ]
    for heading, paragraph, items in _sections(doc, context):
        body.append(f"<h2>{esc(heading)}</h2>")
        if paragraph:
            body.append(f"<p>{esc(paragraph)}</p>")
        elif items:
            body.append("<ul>")
            for item in items:
                li = esc(item.text)
                if item.detail:
                    li += f' <span class="detail">— {esc(item.detail)}</span>'
                if item.times:
                    li += f' <span class="at">({esc(", ".join(item.times))})</span>'
                body.append(f"<li>{li}</li>")
            body.append("</ul>")
        else:
            body.append(f'<p class="none">{esc(none)}</p>')
    return (
        f'<!doctype html><html lang="{HTML_LANG[language]}"><head><meta charset="utf-8">'
        f"<title>{esc(name(doc, context))}</title><style>{STYLE}</style></head>"
        f"<body>{''.join(body)}</body></html>"
    )


# ─── file names ───────────────────────────────────────────────────────────────────────────────────


def filename(context: Context, version: int, extension: str) -> str:
    """`<ascii-title>-minutes-v<version>.<extension>`, letters, digits and dashes only."""
    slug = re.sub(r"[^a-z0-9]+", "-", (context.title or "").lower()).strip("-")[:48].strip("-")
    stem = f"{slug}-minutes" if slug else "minutes"
    if context.meeting_date and not slug:
        stem += f"-{context.meeting_date.isoformat()}"
    return f"{stem}-v{int(version)}.{extension}"


__all__ = ["Context", "clock", "filename", "html", "markdown", "md_escape", "name"]
