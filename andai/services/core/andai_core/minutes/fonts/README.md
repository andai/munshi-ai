# andai_core/minutes/fonts

No font files live here. The minutes PDF (`minutes/pdf.py`) asks for the families
`"Noto Sans", "Noto Sans Devanagari", sans-serif` (`minutes/render.py`), and fontconfig finds them
in the andai-core image, which installs Debian's `fonts-noto-core` (SIL Open Font License 1.1)
next to Pango and HarfBuzz (`services/core/Dockerfile`). Noto Sans covers Latin text and Noto Sans
Devanagari covers Hindi, which HarfBuzz shapes into conjuncts and matras.

The PDF embeds subsets of the fonts it used; `tests/test_mom_export.py` (marker `pdf`, run in the
image) checks that a Hindi document embeds Noto Sans Devanagari and that its text extracts back.
Outside the image (a developer machine without these fonts) a PDF would fall back to whatever
fonts fontconfig has, and may show boxes for Devanagari.
