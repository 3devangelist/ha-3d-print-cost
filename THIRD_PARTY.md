# Third-party code shipped with FDM Printer Manager

## Tesseract.js (text reader for "Read from photo")

Used only when "Read from photo" has no AI Task: the photo is read in the
browser. Up to 0.12.1 the panel downloaded these files from the jsDelivr CDN.
Since 0.12.2 they ship with the integration, in
`custom_components/fdm_printer_manager/frontend/ocr/`, and Home Assistant serves
them itself (`/fdm_printer_manager_static/ocr/...`). Nothing is downloaded from
the internet, and the code never updates on its own: a new version only comes
with a new release of this integration, after review.

The versions are exactly the ones 0.12.1 loaded from the CDN (tesseract.js
5.1.1 asks for tesseract.js-core 5.1.1 and the `4.0.0_best_int` English data).

| Package (npm) | Version | License | Source | npm tarball integrity |
|---|---|---|---|---|
| `tesseract.js` | 5.1.1 | Apache-2.0 | <https://github.com/naptha/tesseract.js> | `sha512-lzVl/Ar3P3zhpUT31NjqeCo1f+D5+YfpZ5J62eo2S14QNVOmHBTtbchHm/YAbOOOzCegFnKf4B3Qih9LuldcYQ==` |
| `tesseract.js-core` | 5.1.1 | Apache-2.0 | <https://github.com/naptha/tesseract.js-core> | `sha512-KX3bYSU5iGcO1XJa+QGPbi+Zjo2qq6eBhNjSGR5E5q0JtzkoipJKOUQD7ph8kFyteCEfEQ0maWLu8MCXtvX5uQ==` |
| `@tesseract.js-data/eng` | 1.0.0 | MIT | <https://github.com/naptha/tessdata> | `sha512-mbTumm6KQPUHyzTPQaF3ObXYnx0SqqfV2nabqFVQBwD6Kl7PhGSLSzOlfFTWy0P3BjghaSKA2W9GB19Jk+ZcTg==` |

Files taken from the packages (byte for byte, not rebuilt or minified again):

| File here | From | SHA-256 |
|---|---|---|
| `tesseract.min.js` | `tesseract.js/dist/tesseract.min.js` | `a8e29918d098b2b06e1012bdaeffb4aec0445c5d5654709023e0bd1f442a80e8` |
| `worker.min.js` | `tesseract.js/dist/worker.min.js` | `aca1229639fc9907d86f96e825955a2b7c5716d17f3bc3acd71f9c7ab66181fc` |
| `core/tesseract-core-simd-lstm.wasm.js` | `tesseract.js-core/tesseract-core-simd-lstm.wasm.js` | `ce20eda9533cbed1e6c2b4276fbae1e0adc61b6754b5513084be601787b457cf` |
| `core/tesseract-core-lstm.wasm.js` | `tesseract.js-core/tesseract-core-lstm.wasm.js` (browsers without WebAssembly SIMD) | `8f04aa0cc81e7bde33f80e92fa01a7a665f0b4884d098acf5de9c7104a11dfaa` |
| `lang/eng.traineddata.gz` | `@tesseract.js-data/eng/4.0.0_best_int/eng.traineddata.gz` | `45b4cb346724ac1774f1c36f42f182b887bcdb28ebe63e6fff90ac41f3fcff91` |
| `LICENSE-tesseract.js.md`, `LICENSE-tesseract.js-core.txt`, `*.LICENSE.txt` | the packages' license files | see `SHA256SUMS` |

`frontend/ocr/SHA256SUMS` lists every file; a test (`tests/test_vendored_ocr.py`)
fails if any file changes, appears or disappears. Total size: about 11 MB.

How the panel uses them (`fdm-printer-panel.js`, `OCR_OPTIONS`): `workerPath`,
`corePath` and `langPath` point to the local files and `workerBlobURL` is off,
so Tesseract.js never falls back to its default CDN addresses.

### Review (2026-11-02)

- Downloaded with `npm pack` from the official npm registry; npm checked each
  tarball against the registry's `sha512` integrity above.
- Package metadata points to the official `naptha` repositories; licenses are
  Apache-2.0 (code) and MIT (language data).
- The only internet addresses in the JavaScript are the jsDelivr defaults
  (`cdn.jsdelivr.net/npm/tesseract.js…`, `…/tesseract.js-core…`,
  `…/@tesseract.js-data/…`), used only when no path is given; the panel always
  gives local paths.
- No `eval`; the one `new Function("return this")` is webpack's standard
  global-object lookup. The core's `XMLHttpRequest` is Emscripten's file loader.

### Updating (by hand, never automatic)

1. `npm pack tesseract.js@<v> tesseract.js-core@<v> @tesseract.js-data/eng@<v>`
2. Copy the files listed above, regenerate `SHA256SUMS`, update this file.
3. Test "Read from photo" in a browser, then release.

## ha-bambulab (reference for the Bambu Lab camera protocol)

`adapters/bambu_camera.py` (0.13.0) talks to the camera of Bambu Lab A1 / P1 printers
on port 6000. No code was copied: the client is our own (asyncio, with a timeout on every
read). The packet layout was learned from reading the open implementation in ha-bambulab
(`pybambu/bambu_client.py`, `ChamberImageThread`): the 80-byte login packet (`0x40, 0x3000,
0, 0`, then `bblp` and the access code padded to 32 bytes) and the 16-byte frame header
(little-endian JPEG size first).

| Project | License | Source |
|---|---|---|
| ha-bambulab | MIT, © 2023 ha-bambulab contributors | <https://github.com/greghesp/ha-bambulab> |
