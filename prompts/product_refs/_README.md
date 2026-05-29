# Per-product reference images

The classification briefs (`prompts/brief_packshot.md` and `prompts/brief_worn.md`) define
named image slots:

- `[Image 1]` — the input 3D render (automatic, never goes here).
- `[Image 2]` — for **packshot** products: optional real-product reference for materials/lighting.
              for **worn** products: clean AI-rendered packshot of the same product (authoritative
              source for jewelry geometry/materials).
- `[Image 3]` — for **packshot**: optional lighting/mood reference.
              for **worn**: skin / lip / makeup reference (per-model, per-product).
- `[Image 4]` — for **worn** only: styling reference (clothing, fabric, color).

## Folder convention

```
prompts/product_refs/
  Skyline/
    image2.png        ← AI packshot of the Skyline (or real product ref for packshot mode)
    image3.png        ← skin/lip/makeup reference (worn mode)
    image4.png        ← styling reference (worn mode)
  Link/
    image2.png
    image3.png
    image4.png
```

The folder name MUST match the product subfolder name in your brand folder.

File names must be `image2.*`, `image3.*`, `image4.*` — extensions `.png`, `.jpg`, `.jpeg`,
`.webp` all work. Any other filenames in the folder are ignored.

## How they're used

When the pipeline generates a prompt or a variant for a photo:
- Claude (system prompt = appropriate brief) receives the input render as [Image 1] and any
  reference images present as [Image 2], [Image 3], [Image 4] — in that order.
- Nano Banana Pro receives the same image set as its conditioning references.

## Editing from the UI

The pipeline UI has a "📎 References" expander at the top of each product section where you
can upload / replace / remove each slot directly. The files land here on disk.
