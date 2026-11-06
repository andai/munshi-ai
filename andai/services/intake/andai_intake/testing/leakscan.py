"""Search files for identifiers of captured mail and print counts only (D-24).

    python -m andai_intake.testing.leakscan --terms FILE PATH [PATH ...]

FILE lists one identifier per line: every address, domain, display name, meeting id and passcode
of the originals (`#` comments and blank lines are skipped). It stays untracked, next to the
originals. `sanitise --terms-out` writes the identifiers it found, and the capture record adds the
rest. Directories under PATH are walked and every file is read, binary files included. Each term
is searched in five forms:

* `plain` — the term as written;
* `lower-case` — the term in any case;
* `url-encoded` — percent-encoded once or twice (either hex case), and with `+` for spaces;
* `url-decoded` — the term percent-decoded, when that differs from the term;
* `base64` — standard and URL-safe alphabets, at each of the three byte offsets, searched with
  white space removed (base64 bodies are wrapped).

A mail file (`.eml`) is also searched as decoded text: each header with encoded words decoded, and
each MIME part after its transfer encoding, with calendar lines unfolded. A term that a
quoted-printable soft break or a folded line splits is still found. File names are searched too.

Output: how many files and terms, hits per form, and for each file with hits its count and the line
numbers of the terms that hit. Terms are never printed. Terms shorter than `MIN_TERM` characters are
skipped and counted. Exit status: 0 with no hits, 1 with hits, 2 on a usage error.
"""

from __future__ import annotations

import argparse
import base64
import os
import re
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from email import message_from_bytes
from email.header import decode_header, make_header
from email.policy import compat32
from pathlib import Path
from urllib.parse import quote, quote_plus, unquote

MIN_TERM = 3
FORMS = ("plain", "lower-case", "url-encoded", "url-decoded", "base64")
SKIP_DIRS = frozenset({".git", "node_modules", "__pycache__", ".venv"})
_URLSAFE = bytes.maketrans(b"+/", b"-_")
_WHITESPACE = re.compile(rb"\s+")


@dataclass(frozen=True)
class Needles:
    """One term's search patterns, per form."""

    line: int
    patterns: dict[str, tuple[re.Pattern[bytes], ...]]


@dataclass
class Report:
    files: int = 0
    terms: int = 0
    skipped: int = 0
    hits: dict[str, int] = field(default_factory=lambda: dict.fromkeys(FORMS, 0))
    per_file: dict[str, tuple[int, set[int]]] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.hits.values())


def load_terms(path: Path) -> tuple[list[tuple[int, str]], int]:
    """(line number, term) of every usable term, and how many were too short."""
    terms: list[tuple[int, str]] = []
    skipped = 0
    for number, line in enumerate(path.read_text("utf-8").splitlines(), start=1):
        term = line.strip()
        if not term or term.startswith("#"):
            continue
        if len(term) < MIN_TERM:
            skipped += 1
            continue
        terms.append((number, term))
    return terms, skipped


def base64_forms(data: bytes) -> set[bytes]:
    """The base64 text that `data` produces at byte offsets 0, 1 and 2, cut to the characters that
    depend only on `data`, in both alphabets."""
    forms: set[bytes] = set()
    for offset in range(3):
        encoded = base64.b64encode(b"\0" * offset + data)
        start = (8 * offset + 5) // 6
        end = (8 * (offset + len(data))) // 6
        fragment = encoded[start:end]
        if len(fragment) >= 4:
            forms.add(fragment)
            forms.add(fragment.translate(_URLSAFE))
    return forms


def _literal(text: str | bytes, *, fold: bool) -> re.Pattern[bytes]:
    data = text.encode("utf-8") if isinstance(text, str) else text
    return re.compile(re.escape(data), re.IGNORECASE if fold else 0)


def needles(line: int, term: str) -> Needles:
    encoded = {quote(term, safe=""), quote(quote(term, safe=""), safe=""), quote_plus(term)}
    encoded.discard(term)
    decoded = unquote(term)
    raw = term.encode("utf-8")
    b64 = base64_forms(raw) | base64_forms(raw.lower())
    patterns = {
        "plain": (_literal(term, fold=False),),
        "lower-case": (_literal(term, fold=True),),
        "url-encoded": tuple(_literal(e, fold=True) for e in sorted(encoded)),
        "url-decoded": (_literal(decoded, fold=True),) if decoded != term else (),
        "base64": tuple(_literal(b, fold=False) for b in sorted(b64)),
    }
    return Needles(line, patterns)


def _decoded_views(data: bytes) -> Iterator[bytes]:
    """A mail file's headers and parts, decoded."""
    try:
        message = message_from_bytes(data, policy=compat32)
    except Exception:  # not mail after all: the raw view is searched anyway
        return
    for part in message.walk():
        for name, value in part.items():
            try:
                text = str(make_header(decode_header(str(value))))
            except (LookupError, ValueError, UnicodeError):
                text = str(value)
            yield f"{name}: {text}".encode("utf-8", "replace")
        if part.is_multipart():
            continue
        payload = part.get_payload(decode=True)
        if isinstance(payload, bytes):
            yield re.sub(rb"\r?\n[ \t]", b"", payload)


def views(path: Path, data: bytes) -> list[tuple[bytes, bool]]:
    """(view, whether it is the white-space-free view for base64) of one file."""
    out = [(data, False), (_WHITESPACE.sub(b"", data), True)]
    if path.suffix.lower() == ".eml":
        for view in _decoded_views(data):
            out += [(view, False), (_WHITESPACE.sub(b"", view), True)]
    out.append((str(path).encode("utf-8", "replace"), False))
    return out


def walk(paths: Iterable[Path]) -> Iterator[Path]:
    for path in paths:
        if path.is_dir():
            for root, dirs, files in os.walk(path):
                dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
                for name in sorted(files):
                    yield Path(root) / name
        elif path.is_file():
            yield path


def scan(terms: list[tuple[int, str]], paths: Iterable[Path], skipped: int = 0) -> Report:
    report = Report(terms=len(terms), skipped=skipped)
    prepared = [needles(line, term) for line, term in terms]
    for path in walk(paths):
        report.files += 1
        try:
            data = path.read_bytes()
        except OSError:
            continue
        count, lines = 0, set()
        for view, compact in views(path, data):
            for needle in prepared:
                for form in FORMS:
                    if compact != (form == "base64"):
                        continue
                    found = sum(len(p.findall(view)) for p in needle.patterns[form])
                    if found:
                        report.hits[form] += found
                        count += found
                        lines.add(needle.line)
        if count:
            report.per_file[str(path)] = (count, lines)
    return report


def render(report: Report) -> str:
    lines = [
        f"leakscan: {report.files} file(s), {report.terms} term(s)"
        + (f" ({report.skipped} skipped: shorter than {MIN_TERM})" if report.skipped else ""),
        *(f"  {form}: {report.hits[form]}" for form in FORMS),
        f"total hits: {report.total}",
    ]
    for path, (count, term_lines) in sorted(report.per_file.items()):
        numbers = ", ".join(str(n) for n in sorted(term_lines))
        lines.append(f"  {path}: {count} hit(s), terms on line(s) {numbers}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m andai_intake.testing.leakscan",
        description="Search files for identifiers; print counts only.",
    )
    parser.add_argument("--terms", required=True, type=Path, help="one identifier per line")
    parser.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args(argv)
    if not args.terms.is_file():
        parser.error("the terms file does not exist")
    terms, skipped = load_terms(args.terms)
    if not terms:
        parser.error("the terms file holds no usable term")
    report = scan(terms, args.paths, skipped)
    print(render(report))
    return 1 if report.total else 0


if __name__ == "__main__":
    sys.exit(main())
