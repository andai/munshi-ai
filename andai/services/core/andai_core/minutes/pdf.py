"""Minutes as PDF (MOM-04): the HTML of `minutes.render` printed by WeasyPrint, offline.

* WeasyPrint is imported inside `render`, not at module import: andai-worker carries andai-core's
  code without Pango, and never renders a PDF. Where WeasyPrint or its system libraries are missing,
  `render` raises `PdfUnavailable` (the export route answers 503 `pdf_unavailable`).
* Nothing is fetched: the URL fetcher refuses every URL (http, https, file, data, anything) before
  any request is made, and the HTML is escaped and self-contained anyway.
* Fonts come from the image through fontconfig: Noto Sans for Latin and Noto Sans Devanagari for
  Hindi (`fonts-noto-core`), so Devanagari is shaped by HarfBuzz into real glyphs, never boxes. See
  `fonts/README.md`.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class PdfUnavailable(RuntimeError):
    """WeasyPrint or the system libraries it loads (Pango, HarfBuzz, fontconfig) are missing."""


class RefusedUrl(ValueError):
    """A URL the PDF renderer was asked to fetch; every URL is refused."""


def _offline_fetcher(url_fetcher_class, refused: list[str] | None):
    class OfflineFetcher(url_fetcher_class):
        """Fetches nothing: every URL is refused before a request is made."""

        def fetch(self, url, headers=None):  # noqa: ARG002 - WeasyPrint's signature
            if refused is not None:
                refused.append(url)
            log.warning("minutes pdf: refused to fetch a %s URL", str(url).split(":", 1)[0])
            raise RefusedUrl("the minutes renderer fetches nothing")

    return OfflineFetcher()


def render(html: str, *, refused: list[str] | None = None) -> bytes:
    """`html` printed to PDF bytes. URLs the document asked for are appended to `refused`."""
    try:
        from weasyprint import HTML
        from weasyprint.urls import URLFetcher
    except (ImportError, OSError) as exc:
        raise PdfUnavailable(f"the PDF renderer cannot load: {exc}") from None
    return HTML(string=html, url_fetcher=_offline_fetcher(URLFetcher, refused)).write_pdf()


__all__ = ["PdfUnavailable", "RefusedUrl", "render"]
