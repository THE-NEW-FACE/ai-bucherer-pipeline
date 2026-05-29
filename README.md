# Auto Pipeline — AI Bucherer

End-to-end automation for turning 3D renders into final visuals.

```
3D render  ─▶  Claude (vision)  ─▶  Nano Banana Pro  ─▶  Harmonizer  ─▶  Saved output
              classify + prompt      N variants            grade preset
```

The pipeline collapses what used to be a manual back-and-forth between Cowork + weavy.ai into a
single Streamlit UI. The existing harmonizer (`_pipeline/harmonizer.py`) is imported and called
programmatically — same math, new orchestration.

## One-time setup

1. **API keys.** Copy `.env.example` to `.env` and fill in:
   ```
   ANTHROPIC_API_KEY=sk-ant-…
   GEMINI_API_KEY=AIza…
   ```
2. **Briefs — one per classification.** Two markdown files in `prompts/`:
   - `brief_packshot.md` — Claude's system prompt for product packshots.
   - `brief_worn.md` — Claude's system prompt for worn / lifestyle renders.

   These are shipped pre-filled with the Bucherer briefs. The classifier (packshot vs worn) is
   `prompts/classifier_prompt.md` — edit only if you want different classification heuristics.

3. **Per-product reference images — worn shots only.** Packshot photos use only the input
   3D render as reference (`[Image 1]`). Worn photos additionally need:
   - `[Image 2]` — clean AI packshot of this product (mandatory for quality)
   - `[Image 3]` — skin / lip / makeup reference (varies per product / model)
   - `[Image 4]` — styling reference (clothing, fabric, color — varies per product)

   Drop them on disk at `prompts/product_refs/<ProductName>/image{2,3,4}.{png,jpg}`, OR
   upload via the **📎 Worn-shot references** expander at the top of each product section in
   the UI. The panel only appears for products that have worn-classified photos (or
   unclassified photos that might become worn).

3. **Install deps** (one-time, already done if you ran the setup):
   ```powershell
   C:\Users\PC\AppData\Local\Programs\Python\Python312\python.exe -m pip install anthropic google-genai python-dotenv pyyaml streamlit pillow numpy
   ```

## Run

Double-click **`run_pipeline.bat`**. Browser opens at http://localhost:8503.

## How to use

**Folder layout the pipeline expects:**

```
Bucherer/                ← point the sidebar at this folder
  Skyline/               ← each subfolder = a product
    photo_01.png         ← each file = a "photo" of that product
    photo_02.png
  Link/
    photo_a.png
    photo_b.png
```

Outputs mirror that exactly into `<brand>_OUT/` (or wherever you point the output folder).

**Workflow:**

1. In the sidebar, paste the **Brand folder** path. Output folder defaults to `<brand>_OUT`.
2. Set **Number of variants** (default 4). Cost meter under the master row shows estimated total.
3. Click **▶ Auto-prepare N pending**. For each photo without variants:
   - Classifies (packshot vs worn) via Claude vision
   - Generates a Nano Banana prompt via Claude using the appropriate brief
   - Generates N variants via Nano Banana Pro
4. Per photo card:
   - **Pick** any variant → it's graded (packshot or worn preset) and saved to the output path
   - **Edit** the prompt textarea (under "Prompt & advanced") then **⟳ Regenerate variants**
   - **Swap reference image** to feed Nano Banana something different from the 3D render
   - Flip the classification dropdown if Claude got it wrong — affects the grading preset
5. View modes (sidebar):
   - **Side by side** — input | variants | saved output (default, best for QC)
   - **Input only** / **Output only** — clean comparison boards
6. **🧹 Tidy workspace** when done — deletes the throwaway variants for photos whose output
   is already saved on disk. Keeps the brand folder lean.

**State persists.** Close the app, reopen, point at the same brand folder — manifest reloads,
selections + prompts + grading status all preserved.

## Files

```
_auto_pipeline/
├── app.py                    ← Streamlit UI
├── config.yaml               ← defaults (N, models, costs, RPM cap)
├── .env                      ← your API keys (gitignored)
├── .env.example
├── run_pipeline.bat          ← launcher
├── prompts/
│   ├── classifier_prompt.md  ← fixed packshot/worn detector
│   ├── brief_packshot.md     ← system prompt for packshot generation
│   ├── brief_worn.md         ← system prompt for worn-shot generation
│   └── product_refs/
│       ├── _README.md        (convention)
│       └── <ProductName>/
│           ├── image2.png    ← [Image 2] reference
│           ├── image3.png    ← [Image 3] reference
│           └── image4.png    ← [Image 4] reference
└── src/
    ├── config.py             ← loads .env + yaml
    ├── manifest.py           ← state on disk
    ├── grader.py             ← wraps _pipeline/harmonizer.py headlessly
    ├── anthropic_client.py   ← Claude (vision + prompt caching)
    ├── gemini_client.py      ← Nano Banana Pro (threaded N-variants)
    └── pipeline.py           ← orchestrator: ingest, classify, generate, grade
```

## Output settings

Defaults in `config.yaml`:

- **Model:** `gemini-3-pro-image-preview` (Nano Banana Pro — best available)
- **Aspect ratio:** `1:1`
- **Image size:** `2K`

1K and 2K are billed the same per the Gemini docs, so 2K is a free upgrade. 4K costs ~2× more.
Edit `gemini.aspect_ratio` and `gemini.image_size` in `config.yaml` for different outputs;
supported values:
- Aspect ratios: `1:1`, `2:3`, `3:2`, `3:4`, `4:3`, `4:5`, `5:4`, `9:16`, `16:9`, `21:9`
- Sizes: `1K`, `2K`, `4K`

## Costs

Rough estimate at default settings (4 variants, 2K, single product):

- Per photo:
  - Classify (Claude vision): ~$0.005
  - Prompt (Claude vision, cached brief): ~$0.005
  - 4 variants (Nano Banana Pro 2K): 4 × ~$0.05 = $0.20
  - **Total: ~$0.21 per photo**
- Batch of 20 photos: ~$4
- The cost meter in the sidebar tracks real spend; update `gemini.cost_per_image` in
  `config.yaml` once you have a few real bills to compare.

To halve image cost, set `gemini.batch_tier: true` in `config.yaml`. Slower turnaround.

## Troubleshooting

- **"ANTHROPIC_API_KEY missing"** — check `.env` exists in `_auto_pipeline/` and has no quotes around the value. Restart the app after editing `.env`.
- **"Brief missing for classification '<X>'"** — One of `prompts/brief_packshot.md` or `prompts/brief_worn.md` is empty. Restore the file content from version control or paste your brief back in.
- **Worn shots come out weak / generic** — You probably haven't added the [Image 2/3/4] references for that product. Use the **📎 References** expander at the top of the product section to upload them. Without skin and styling references, the worn brief falls back to text-only descriptions, which is much less reliable.
- **Gemini call failed after 3 attempts** — check rate limits (default cap 200 RPM under tier-1 ceiling of 300), or the model name in `config.yaml`. Retries already do exponential backoff.
- **Variants look wrong / not on-brand** — refine `packshot_brief.md`. The brief is the single biggest lever on output quality.
- **Streamlit app port already in use** — change the port in `run_pipeline.bat` (`--server.port=8503` → another number).
- **Output looks bad after grading** — flip to the harmonizer's own UI (`_pipeline/run_harmonizer.bat`) for that image; the auto pipeline uses simple presets, the harmonizer gives you per-image sliders.

## Limits / out of scope (v1)

- No auto-pick of best variant (intentional — manual review is the QC gate).
- No multi-brand comparison view.
- No SynthID-watermark removal (it's non-visible; visible Gemini sparkle requires account tier change).
- Hero-image-driven LAB sampling not exposed here — use the harmonizer UI when you need full creative control.
- Regenerate overwrites previous variants; no version history kept.
