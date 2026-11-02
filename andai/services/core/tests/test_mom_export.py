"""Minutes downloads (MOM-04): Markdown and PDF laid out by the document's template in its output
language, with each item's evidence as transcript times; the PDF printed offline with real
Devanagari glyphs.

Two kinds of test live here:

* Unmarked tests run with the andai suite (database in Docker): the Markdown for every template ×
  language, escaping, file names, and the two export routes (the PDF route with the renderer
  stubbed, so the host needs no Pango).
* `pdf` tests need WeasyPrint's system libraries and the Noto fonts, so they run inside the
  andai-core image (excluded by default; `-m pdf`, with pytest and pypdf added for the run). They
  use no database, and they fail, never skip, when WeasyPrint cannot load.
"""

from __future__ import annotations

import io
import re
import uuid
from datetime import UTC, date, datetime

import pytest

from andai_core.db.models import Meeting
from andai_core.db.session import transaction
from andai_core.meetings import mom
from andai_core.meetings.title import set_title
from andai_core.minutes import pdf, render, templates
from andai_core.minutes.render import Context
from conftest import access_org, mom_body, seed_segments

SEGMENTS = {"seg_a": 5.0, "seg_b": 65.4, "seg_c": 3725.0}
HINDI_SUMMARY = "बजट की समीक्षा शुक्रवार तक पूरी होगी और क्षेत्रीय टीम रिपोर्ट भेजेगी।"


def document(template_id: str = "standard", language: str = "en", **overrides) -> dict:
    """A stored SPEC `MomDocument` body (version 3) citing the three segments of `SEGMENTS`."""
    texts = {
        "en": ("Budget review by Friday.", "Ship on Friday", "Send the budget sheet", "Vendor"),
        "hi": (HINDI_SUMMARY, "शुक्रवार को रिलीज़", "बजट शीट भेजना", "विक्रेता"),
        "hinglish": (
            "Budget review Friday tak.", "Friday ko ship", "Budget sheet bhejna", "Vendor",
        ),
    }[language]
    doc = {
        "meetingId": str(uuid.uuid4()),
        "version": 3,
        "templateId": template_id,
        "outputLanguage": language,
        "summary": texts[0],
        "decisions": [{"text": texts[1], "evidence": ["seg_a"]}],
        "actionItems": [
            {
                "id": str(uuid.uuid4()),
                "text": texts[2],
                "owner": "Ravi",
                "due": "2026-11-06",
                "evidence": ["seg_b", "seg_a"],
            },
            {
                "id": str(uuid.uuid4()),
                "text": texts[3],
                "owner": "unassigned",
                "evidence": ["seg_c"],
            },
        ],
        "topics": [{"title": texts[3], "evidence": ["seg_c"]}],
        "risks": [],
        "llmProfileId": str(uuid.uuid4()),
        "model": "test-model",
        "generatedAt": datetime(2026, 11, 2, 10, 0, tzinfo=UTC).isoformat(),
    }
    doc.update(overrides)
    return doc


CONTEXT = Context(
    title="Budget review", meeting_date=date(2026, 11, 2), attendees=["Ravi", "Asha"],
    times=SEGMENTS,
)  # fmt: skip


# ─── Markdown ─────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("language", templates.LANGUAGES)
@pytest.mark.parametrize("template_id", sorted(templates.TEMPLATES))
def test_markdown_follows_the_template_in_the_language(template_id: str, language: str) -> None:
    doc = document(template_id, language)
    template = templates.get(template_id)

    text = render.markdown(doc, CONTEXT)

    headings = re.findall(r"^## (.+)$", text, flags=re.MULTILINE)
    assert headings == [
        render.md_escape(template.heading(s, language)) for s in template.sections
    ]
    assert text.startswith("# Budget review\n")
    assert render.md_escape(template.name(language)) in text
    assert render.md_escape(doc["summary"]) in text
    # Every item, with the times of the segments it cites (sorted, mm:ss past the hour).
    assert f"- {render.md_escape(doc['decisions'][0]['text'])} (00:05)" in text
    action = render.md_escape(doc["actionItems"][0]["text"])
    assert re.search(rf"^- {re.escape(action)} — Ravi, .+ 2026-11-06 \(00:05, 01:05\)$",
                     text, flags=re.MULTILINE)  # fmt: skip
    unassigned = render.md_escape(templates.UNASSIGNED_LABEL[language])
    assert f"— {unassigned} (62:05)" in text
    assert f"- {render.md_escape(doc['topics'][0]['title'])} (62:05)" in text
    assert "- Ravi\n- Asha" in text
    assert f"_{render.md_escape(templates.NONE_RECORDED[language])}_" in text  # risks: none


def test_markdown_escapes_what_markdown_would_act_on() -> None:
    doc = document(
        summary="**bold** [link](http://evil.test) <img src=x> # not a heading",
        decisions=[{"text": "- not a list", "evidence": ["seg_a"]}],
    )

    text = render.markdown(doc, CONTEXT)

    assert "\\*\\*bold\\*\\* \\[link\\]\\(http://evil.test\\) \\<img src=x\\> \\# not" in text
    assert "- \\- not a list (00:05)" in text
    assert "](http" not in text and "<img" not in text.replace("\\<img", "")


def test_an_untitled_meeting_is_named_by_its_date_in_the_language() -> None:
    untitled = Context(title="  ", meeting_date=date(2026, 11, 2), times=SEGMENTS)

    assert render.markdown(document(language="hi"), untitled).startswith(
        "# 2026-11-02 की मीटिंग\n"
    )
    assert render.filename(untitled, 3, "pdf") == "minutes-2026-11-02-v3.pdf"


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Budget review", "budget-review-minutes-v3.md"),
        ("बजट समीक्षा", "minutes-2026-11-02-v3.md"),
        ('Q4 "plan"; rm -rf / \r\nX: y', "q4-plan-rm-rf-x-y-minutes-v3.md"),
        ("बजट review — Q4", "review-q4-minutes-v3.md"),
    ],
)
def test_file_names_are_ascii_safe(title: str, expected: str) -> None:
    name = render.filename(Context(title=title, meeting_date=date(2026, 11, 2)), 3, "md")

    assert name == expected
    assert re.fullmatch(r"[a-z0-9-]+\.md", name)


def test_html_escapes_every_value() -> None:
    doc = document(summary='<img src="http://evil.test/x.png"> & <script>alert(1)</script>')

    page = render.html(doc, Context(title="<b>t</b>", meeting_date=None, times=SEGMENTS))

    assert "<img" not in page and "<script" not in page and "<b>" not in page
    assert "&lt;img src=&quot;http://evil.test/x.png&quot;&gt; &amp; &lt;script&gt;" in page
    assert 'lang="en"' in page


# ─── the routes ───────────────────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def world(app_engine):
    w = access_org(app_engine)
    w.meeting_id = w.meeting(started_at=datetime(2026, 11, 2, 5, 0, tzinfo=UTC))
    ids = seed_segments(
        app_engine, w.org_id, w.meeting_id,
        [("Asha", "कल तक deadline है"), ("Ravi", "main bhej dunga"), ("Asha", "vendor")],
    )  # fmt: skip
    with transaction(app_engine, org_id=w.org_id) as tx:
        set_title(tx, tx.get(Meeting, w.meeting_id), "Budget review — बजट")
        mom.seed_version(tx, w.org_id, w.meeting_id, mom_body(ids, summary="pehla draft"))
        mom.seed_version(
            tx, w.org_id, w.meeting_id,
            mom_body(
                ids, templateId="indian_formal_mom", outputLanguage="hi", summary=HINDI_SUMMARY
            ),
        )  # fmt: skip
    return w


def _path(meeting_id, tail: str) -> str:
    return f"/api/v1/meetings/{meeting_id}/mom/{tail}"


def test_the_markdown_download_is_the_latest_version(world) -> None:
    response = world.client("user_grantee").get(_path(world.meeting_id, "export.md"))

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "text/markdown; charset=utf-8"
    assert response.headers["content-disposition"] == (
        'attachment; filename="budget-review-minutes-v2.md"'
    )
    assert response.headers["cache-control"] == "no-store"
    text = response.text
    assert text.startswith("# Budget review — बजट\n")
    assert HINDI_SUMMARY in text
    assert "## लिए गए निर्णय" in text
    assert "(00:00)" in text and "(00:05)" in text  # seeded segments start at 0 s and 5 s
    assert "2026-11-02" in text  # the meeting's date in Asia/Kolkata


def test_any_version_downloads(world) -> None:
    client = world.client("requester")

    first = client.get(_path(world.meeting_id, "export.md"), params={"version": 1})
    missing = client.get(_path(world.meeting_id, "export.md"), params={"version": 9})

    assert first.status_code == 200 and "pehla draft" in first.text
    assert "## Summary" in first.text  # version 1 is standard, Hinglish
    assert first.headers["content-disposition"].endswith('-v1.md"')
    assert missing.status_code == 404 and missing.json()["code"] == "no_such_version"


def test_the_pdf_download_prints_the_versions_html(world, monkeypatch) -> None:
    printed: list[str] = []

    def fake_render(html: str, **_) -> bytes:
        printed.append(html)
        return b"%PDF-1.7 stub"

    monkeypatch.setattr(pdf, "render", fake_render)

    response = world.client("team_grantee").get(_path(world.meeting_id, "export.pdf"))

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"] == (
        'attachment; filename="budget-review-minutes-v2.pdf"'
    )
    assert response.content == b"%PDF-1.7 stub"
    [page] = printed
    assert 'lang="hi"' in page and HINDI_SUMMARY in page and "लिए गए निर्णय" in page


def test_a_server_without_the_pdf_renderer_answers_503(world, monkeypatch) -> None:
    def unavailable(html: str, **_) -> bytes:
        raise pdf.PdfUnavailable("no pango")

    monkeypatch.setattr(pdf, "render", unavailable)

    response = world.client("requester").get(_path(world.meeting_id, "export.pdf"))

    assert response.status_code == 503 and response.json()["code"] == "pdf_unavailable"


# ─── PDF (in the andai-core image) ────────────────────────────────────────────────────────────────


def _pdf_fonts(data: bytes) -> set[str]:
    from pypdf import PdfReader

    fonts: set[str] = set()
    for page in PdfReader(io.BytesIO(data)).pages:
        for font in (page["/Resources"].get("/Font") or {}).values():
            # "/ABCDEF+Noto-Sans-Devanagari" (a subset) → "NotoSansDevanagari"
            fonts.add(str(font.get_object()["/BaseFont"]).split("+")[-1].replace("-", ""))
    return fonts


def _pdf_text(data: bytes) -> str:
    from pypdf import PdfReader

    return "".join(page.extract_text() for page in PdfReader(io.BytesIO(data)).pages)


@pytest.mark.pdf
@pytest.mark.parametrize("language", templates.LANGUAGES)
@pytest.mark.parametrize("template_id", sorted(templates.TEMPLATES))
def test_every_template_and_language_prints(template_id: str, language: str) -> None:
    data = pdf.render(render.html(document(template_id, language), CONTEXT))

    assert data.startswith(b"%PDF")
    fonts = _pdf_fonts(data)
    assert any("NotoSans" in f for f in fonts), fonts
    if language == "hi":
        assert any("NotoSansDevanagari" in f for f in fonts), fonts


# Devanagari words that read back from the PDF exactly. Text extraction (the PDF's ToUnicode map,
# one text per glyph) cannot give back a pre-base matra (ि) or a reph (र्) in reading order, so
# words with those are left out here; how they look is checked by eye.
READ_BACK = ("बजट", "समीक्षा", "शुक्रवार", "क्षे", "टीम", "भेजेगी")


@pytest.mark.pdf
def test_hindi_text_is_real_devanagari_glyphs() -> None:
    data = pdf.render(render.html(document("indian_formal_mom", "hi"), CONTEXT))

    assert any("NotoSansDevanagari" in f for f in _pdf_fonts(data))
    text = re.sub(r"\s+", "", _pdf_text(data))
    for word in (*READ_BACK, "उपस्थितसदस्य", "लिएगए", "बजटशीट"):
        assert word in text, word
    assert "\ufffd" not in text


@pytest.mark.pdf
def test_hinglish_with_devanagari_content_embeds_the_devanagari_font() -> None:
    doc = document(language="hinglish", summary=f"Budget review: {HINDI_SUMMARY}")

    data = pdf.render(render.html(doc, CONTEXT))

    assert any("NotoSansDevanagari" in f for f in _pdf_fonts(data))
    text = re.sub(r"\s+", "", _pdf_text(data))
    assert "Budgetreview:बजटकीसमीक्षाशुक्रवारतकपूरीहोगी" in text
    for word in READ_BACK:
        assert word in text, word


@pytest.mark.pdf
def test_the_renderer_fetches_nothing(monkeypatch) -> None:
    import socket

    def no_network(*args, **kwargs):
        raise AssertionError("the PDF renderer opened a connection")

    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    refused: list[str] = []
    page = (
        '<html><head><link rel="stylesheet" href="https://evil.test/s.css"></head><body>'
        '<p>x</p><img src="http://evil.test/pixel.png"><img src="file:///etc/passwd">'
        '<p style="background: url(http://evil.test/bg.png)">y</p></body></html>'
    )

    data = pdf.render(page, refused=refused)

    assert data.startswith(b"%PDF")
    assert set(refused) >= {
        "https://evil.test/s.css", "http://evil.test/pixel.png", "file:///etc/passwd",
    }  # fmt: skip


@pytest.mark.pdf
def test_an_image_injected_into_a_summary_is_text_not_a_fetch() -> None:
    refused: list[str] = []
    doc = document(summary='<img src="http://evil.test/pixel.png"> summary')

    data = pdf.render(render.html(doc, CONTEXT), refused=refused)

    assert refused == []
    assert 'img src="http://evil.test/pixel.png"' in _pdf_text(data)
