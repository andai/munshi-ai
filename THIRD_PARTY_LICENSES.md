# Third-party licenses — baked artifacts outside the dependency gate

`gate:licenses` (ADR-0004, `scripts/gates.mjs`) scans the resolved **npm** dependency
tree (`pnpm licenses list --json`) and the Python tree grows into `pip-licenses`. Neither
sees **non-dependency artifacts baked into the images** — model weights pulled from a model
hub, or a service binary built from source at image-build time. This file is the
packaging-side complement: every such artifact is recorded here with its license, mirrored
into the per-release SPDX SBOM (`scripts/sbom.mjs`), and — for baked binaries and pinned
container images — audited by **`gate:image-licenses`** against
[`image-licenses.json`](image-licenses.json) (#653).

The verbatim upstream license for each entry lives under [`licenses/`](licenses/) and is
copied into the image next to the artifact it covers, so the notice travels with the bytes.

The SPDX also inventories every package name in the Lite Dockerfile's final-stage `apt install`.
That is a source-level inventory: Docker source does not resolve Ubuntu package versions or
package licences, so those fields are emitted as `NOASSERTION` until an image-level scanner
enriches them. Builder-stage packages that do not cross into the final image are excluded.

## Baked service binaries

| Artifact | Version | License | Source | Baked into | In-image path |
| --- | --- | --- | --- | --- | --- |
| `valkey` | `8.1.9` | BSD-3-Clause | [github.com/valkey-io/valkey](https://github.com/valkey-io/valkey) | `ghcr.io/andai/munshi-lite` | `/usr/local/bin/valkey-server`, `/usr/local/share/valkey/LICENSE` |

**`valkey`** — the internal cache / stream store (bus, scheduler, per-dispatch streams). Lite
builds `valkey-server` + `valkey-cli` from the pinned git tag in the `valkey-builder` stage of
[`deploy/lite/Dockerfile.lite`](deploy/lite/Dockerfile.lite) (glibc-matched to the final image;
an alpine/musl binary can't run there) and copies them onto `PATH`. Valkey is the Linux
Foundation's BSD-3-Clause fork of Redis 7.2.4 — it has `XAUTOCLAIM` (parity with compose/helm)
and keeps Lite off both jammy apt's stale Redis 6.0.16 and source-available Redis ≥7.4
(RSALv2/SSPLv1). compose and helm pin `valkey/valkey:8-alpine` (operator-pulled, recorded in
[`image-licenses.json`](image-licenses.json)). Full text:
[`licenses/valkey-8.1.9.COPYING.txt`](licenses/valkey-8.1.9.COPYING.txt).

> BSD-3-Clause is a Category-A (permissive) license under ADR-0004; baking it requires no
> exception, only that the notice be preserved — which this file and the in-image
> `/usr/local/share/valkey/LICENSE` satisfy.

## Baked model weights

| Artifact | Version (revision) | License | Source | Baked into | In-image path |
| --- | --- | --- | --- | --- | --- |
| `onnx-community/pyannote-segmentation-3.0` | `main` | MIT | [huggingface.co](https://huggingface.co/onnx-community/pyannote-segmentation-3.0) | `ghcr.io/andai/munshi-bot`, `ghcr.io/andai/munshi-lite` | `/opt/hf-cache/LICENSE.pyannote-segmentation-3.0` |

**`onnx-community/pyannote-segmentation-3.0`** — the mixed (Zoom/Teams) speaker-diarization
lane segments speakers with this model, loaded OFFLINE from an image-baked HuggingFace cache
at `/opt/hf-cache` (see [`warm-hf-cache.mjs`](core/meetings/services/bot/warm-hf-cache.mjs)).
It is an ONNX conversion of the gated [`pyannote/segmentation-3.0`](https://huggingface.co/pyannote/segmentation-3.0);
the conversion is published MIT and **not** gated. The model card ships no LICENSE file of
its own, so the preserved notice is the [pyannote.audio](https://github.com/pyannote/pyannote-audio)
project's MIT license (`Copyright (c) 2020 CNRS`), whose copyright governs the weights. Full
text: [`licenses/onnx-community-pyannote-segmentation-3.0.LICENSE.txt`](licenses/onnx-community-pyannote-segmentation-3.0.LICENSE.txt).

> MIT is a Category-A (permissive) license under ADR-0004; baking it requires no exception,
> only that the copyright + permission notice be preserved — which this file and the baked
> `/opt/hf-cache/LICENSE.*` satisfy.

## andai-core: minutes PDF export

The minutes downloads (`andai_core.minutes.pdf`) print HTML to PDF with WeasyPrint, offline. The
Python packages are pinned in `andai/uv.lock` and installed from PyPI into the andai-core
virtualenv (andai-worker carries them too, as andai-core's dependencies, but never renders a PDF).
`gate:licenses` does not read the Python tree, so they are listed here.

| Package | Version | License | Why |
| --- | --- | --- | --- |
| `weasyprint` | `70.0` | BSD-3-Clause | HTML/CSS to PDF |
| `pydyf` | `0.13.0` | BSD-3-Clause | PDF writer (WeasyPrint) |
| `tinycss2` | `1.5.1` | BSD-3-Clause | CSS parser (WeasyPrint) |
| `cssselect2` | `0.10.1` | BSD-3-Clause | CSS selectors (WeasyPrint) |
| `tinyhtml5` | `2.1.0` | MIT | HTML parser (WeasyPrint) |
| `webencodings` | `0.6.1` | BSD-3-Clause | character encodings (tinycss2, tinyhtml5) |
| `cffi` | `2.1.1` | MIT-0 | loads Pango and HarfBuzz (WeasyPrint) |
| `pycparser` | `3.12` | BSD-3-Clause | C parser (cffi) |
| `pillow` | `13.0.0` | MIT-CMU | images (WeasyPrint) |
| `fonttools` (`woff`) | `4.68.0` | MIT | font subsetting (WeasyPrint) |
| `brotli`, `brotlicffi` | `1.2.1`, `1.2.1.0` | MIT | WOFF2 (fonttools) |
| `zopfli` | `0.4.3` | Apache-2.0 | WOFF compression (fonttools) |
| `pyphen` | `0.18.1` | GPL-2.0+ OR LGPL-2.1+ OR MPL-1.1 | hyphenation (WeasyPrint); used under LGPL/MPL, logged in `license-exceptions.json` |

`pypdf` `6.20.1` (BSD-3-Clause) is a development dependency of the andai workspace (it reads the
PDFs back in `tests/test_mom_export.py`) and is not in any image.

The andai-core image's runtime stage installs these Debian bookworm packages for WeasyPrint
(`andai/services/core/Dockerfile`); each keeps its Debian copyright file under
`/usr/share/doc/<package>/copyright` in the image.

| Debian package (and what apt pulls in) | Version | License |
| --- | --- | --- |
| `libpango-1.0-0`, `libpangoft2-1.0-0` | `1.50.12+ds-1` | LGPL-2.1+ (logged in `license-exceptions.json` with GLib, FriBidi, libthai, libdatrie, graphite2) |
| `libharfbuzz0b`, `libharfbuzz-subset0` | `6.0.0+dfsg-3` | MIT (HarfBuzz "Old MIT") |
| `fontconfig`, `libfontconfig1` | `2.14.1-4` | MIT-style (fontconfig) |
| `libfreetype6` | `2.12.1+dfsg-5+deb12u4` | FreeType License (FTL) |
| `libpng16-16`, `libexpat1`, `libbrotli1` | bookworm | libpng / MIT / MIT |
| `fonts-noto-core` | `20201225-1` | SIL Open Font License 1.1 (Noto Sans, Noto Sans Devanagari) |
| `fonts-dejavu-core` | `2.37-6` | Bitstream Vera (fontconfig's default font) |

> The fonts are OFL-1.1 and Bitstream Vera, both Category A for fonts: they may be bundled and
> redistributed with software provided they are not sold on their own, and their notices travel
> with them in the image.

## andai-intake: SMTP receiver

andai-intake receives the notetaker mailbox's mail with aiosmtpd (`andai_intake.smtp`). The packages
are pinned in `andai/uv.lock` and installed from PyPI into the andai-intake virtualenv, which also
carries andai-core and its dependencies (listed above and in `license-exceptions.json`).

| Package | Version | License | Why |
| --- | --- | --- | --- |
| `aiosmtpd` | `1.4.6` | Apache-2.0 | asyncio SMTP server (ships its own `NOTICE`) |
| `atpublic` | `9.0.0` | Apache-2.0 | `@public` decorator (aiosmtpd) |
| `attrs` | `26.1.0` | MIT | classes without boilerplate (aiosmtpd) |

## andai-intake: sender verification

andai-intake checks each sender itself (D-56): SPF with pyspf, DKIM and ARC with dkimpy, and the
ARC hop's results with authres (`andai_intake.auth`). The DNS lookups go through dnspython. The
packages are pinned in `andai/uv.lock` and installed from PyPI into the andai-intake virtualenv.
All four licences are permissive. Zlib and the PSF licence allow use, change and redistribution;
Zlib asks only that its notice is kept in source distributions, and the installed package carries it.

| Package | Version | License | Why |
| --- | --- | --- | --- |
| `dkimpy` (`arc` extra) | `1.1.8` | Zlib | DKIM and ARC verification |
| `pyspf` | `2.0.14` | PSF-2.0 | SPF checks |
| `authres` | `1.2.0` | Apache-2.0 | parses the ARC-Authentication-Results of a sealing hop |
| `dnspython` | `2.9.0` | ISC | DNS lookups (also a dependency of `email-validator` in andai-core) |
