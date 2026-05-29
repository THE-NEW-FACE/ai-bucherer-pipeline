"""Worn-render styling parameters.

The worn-render prompt is produced in two stages:

  1. ANALYZER (Claude vision, prompts/worn_analyzer.md) — looks at the 3D render once
     and returns a *templated* Nano Banana prompt that leaves six styling
     placeholders literal: {SKIN_TONE} {MAKEUP} {HAIR_STYLE} {HAIR_COLOR}
     {CLOTHING} {CLOTHING_MATERIAL}. Cached on the photo (re-run only when the
     render itself changes).

  2. ASSEMBLER (pure Python, `assemble` below) — fills those placeholders from the
     option descriptions here, based on the user's per-photo selections. Instant,
     no API cost — re-run every time the user tweaks a parameter.

Options are sourced from the Bucherer brief PDF. Each option carries a display
label, a 512×512 thumbnail (under prompts/worn_thumbnails/), and the exact
description string injected verbatim into the prompt. To add options beyond the
brief, append entries following the same shape.
"""

from __future__ import annotations

import random
from pathlib import Path

# Order matters for the UI (top-to-bottom) and for display grouping.
PARAMS: tuple[str, ...] = (
    "SKIN_TONE",
    "MAKEUP",
    "HAIR_STYLE",
    "HAIR_COLOR",
    "CLOTHING",
    "CLOTHING_MATERIAL",
)

# Human-friendly group titles for the UI.
PARAM_LABELS: dict[str, str] = {
    "SKIN_TONE": "Skin tone",
    "MAKEUP": "Makeup",
    "HAIR_STYLE": "Hair style",
    "HAIR_COLOR": "Hair color",
    "CLOTHING": "Clothing (cut)",
    "CLOTHING_MATERIAL": "Clothing (fabric + color)",
}

# Per-parameter options: key → {label, thumb, desc}. `desc` is the verbatim string
# that replaces the {PLACEHOLDER}. `thumb` is a filename under prompts/worn_thumbnails/.
OPTIONS: dict[str, dict[str, dict[str, str]]] = {
    "SKIN_TONE": {
        "fair_warm_caucasian": {
            "label": "Fair Warm Caucasian",
            "thumb": "skin_tone__fair_warm_caucasian.jpg",
            "desc": "Photoreal fair-light Caucasian skin with a soft warm peachy undertone — healthy, glowy, luminous; NOT pale-cool, NOT sallow, NOT yellow. Skin texture is SMOOTH and refined — very fine pores, soft peach fuzz, subtle subsurface scattering, faint natural skin variation across the décolleté, neck, chest, and arms. Editorial-beauty smoothness — NOT a blur filter, NOT plastic, NOT waxy, NOT airbrushed CG; fine micro-detail still resolves. Natural micro-tone variation — slightly warmer at the apple of the cheek and the bridge of the nose, slightly cooler at the temples. Healthy luminous glow on the cheekbones, brow bone, and chin with soft catchlights, no shine.",
        },
        "medium_deep_international_brown": {
            "label": "Medium-Deep International Brown",
            "thumb": "skin_tone__medium_deep_international_brown.jpg",
            "desc": "Photoreal medium-deep brown skin with a warm golden-bronze neutral undertone — international, balanced, never too dark and never too light; healthy luminous quality, never ashy, never gray, never matte-flat. Visible fine pores, soft peach fuzz, subtle subsurface scattering, faint natural skin variation across the décolleté, neck, chest, and arms. Smooth refined editorial-beauty texture — NOT plastic, NOT waxy, NOT a blur filter, NOT airbrushed CG; fine micro-detail still resolves on close inspection. Natural micro-tone variation across the facial planes — slightly warmer at the apple of the cheek, slightly cooler at the temples. Healthy warm golden glow on the cheekbones, brow bone, and chin with soft catchlights, no shine, no hot spots.",
        },
        "fair_pale_pink_blush": {
            "label": "Fair Pale with Pink Blush",
            "thumb": "skin_tone__fair_pale_pink_blush.jpg",
            "desc": "Photoreal pale fair Caucasian skin with a soft cool-neutral undertone — clearly pale and luminous, but NOT extreme porcelain, NOT chalky, NOT lifeless. A clear, naturally visible soft pink blush across the apples of the cheeks — diffuse, naturally blended, the gentle natural flush of a real fair-skinned person; visible enough to bring warmth and life to the otherwise pale complexion, NEVER stripey, NEVER heavy, NEVER applied-looking. Skin texture is SMOOTH and refined — very fine, barely-perceptible pores, soft peach fuzz, gentle subsurface scattering, faint natural skin variation across the décolleté, neck, chest, and arms. Editorial-beauty smoothness — NOT a blur filter, NOT plastic, NOT waxy, NOT airbrushed CG; fine micro-detail still resolves on close inspection. Subtle natural luminosity on the cheekbones, brow bone, and chin — soft glow with gentle catchlights from the key light, no shine, no hot spots.",
        },
    },
    "MAKEUP": {
        "editorial_bronze_glow": {
            "label": "Editorial Bronze Glow",
            "thumb": "makeup__editorial_bronze_glow.jpg",
            "desc": "Editorial bronze-glow beauty for warmer / deeper skin tones. Lips full and hydrated in a natural warm nude-brown tone with a subtle gentle gloss — visible sheen catching the key light, NOT wet, NOT lacquered, NOT plastic. A subtle warm bronze cheek warmth across the cheekbones, diffuse and naturally blended (NOT pink — bronze warmth reads correctly on warm and deep skin tones). Softly defined natural brows with individual hair detail. Soft mascara on upper lashes only, no eyeliner. Complexion luminous and glowing with natural warm radiance.",
        },
        "glossy_lip_beauty": {
            "label": "Glossy Lip Beauty",
            "thumb": "makeup__glossy_lip_beauty.jpg",
            "desc": "Clean beauty makeup with a glossy-lip emphasis. Lips FULL and PLUMP, hydrated, in a soft pinkish-coral tone with a definite gentle gloss — visible sheen catching the key light, hydrated and dewy, NOT wet-plastic, NOT lacquered. Subtle peachy warmth across the apples of the cheeks, diffuse and natural. Softly defined natural brows. Minimal mascara on upper lashes only, no eyeliner. Complexion clean, smooth, and luminous with a healthy glow.",
        },
    },
    "HAIR_STYLE": {
        "low_ponytail_natural_textured": {
            "label": "Low Ponytail Natural Textured",
            "thumb": "hair_style__low_ponytail_natural_textured.jpg",
            "desc": "Hair pulled back into a low ponytail at the nape of the neck, natural texture preserved — soft individual strand definition with visible natural coily or wavy structure, slight escape hairs at the nape adding realism. Crown smooth and pulled back cleanly, fully clearing the neckline, jawline, ears, and shoulders (hair must NEVER block the ear). Natural soft hairline transition with fine baby hairs / wisps visible at the temples and front hairline where skin meets hair — NEVER a sharp synthetic edge, NEVER flat-airbrushed.",
        },
        "slicked_back_sleek": {
            "label": "Slicked Back Sleek",
            "thumb": "hair_style__slicked_back_sleek.jpg",
            "desc": "Hair slicked back sleek and polished against the crown, fully clearing the neckline, jawline, ears, and shoulders (hair must NEVER block the ear). NOT wet, NOT greasy — a clean polished editorial finish with soft glossy sheen. Subtle individual strand detail visible across the smooth pulled-back surface. Natural soft hairline transition with fine baby hairs / wisps at the temples and front hairline.",
        },
        "down_and_back": {
            "label": "Down And Back",
            "thumb": "hair_style__down_and_back.jpg",
            "desc": "Hair worn down behind the shoulders, smooth and natural, falling past the shoulders behind the back so the front of the neckline, jawline, ears, and shoulders remain fully clear and the jewelry is unobstructed (hair must NEVER block the ear). Soft individual strand detail, soft natural sheen, NOT overly styled, NOT stiff, NOT flattened. Natural soft hairline transition with fine baby hairs at the temples and front hairline.",
        },
    },
    "HAIR_COLOR": {
        "natural_black": {
            "label": "Natural Black",
            "thumb": "hair_color__natural_black.jpg",
            "desc": "deep saturated natural black hair, rich and luminous with subtle highlights catching the key light, soft glossy sheen, never flat-matte, never blue-tinted plastic.",
        },
        "dark_brown": {
            "label": "Dark Brown",
            "thumb": "hair_color__dark_brown.jpg",
            "desc": "natural dark brown hair with deep cool-warm balanced undertones, soft glossy sheen catching the key light, individual strand detail visible.",
        },
    },
    "CLOTHING": {
        "sleeveless_high_neck_tank": {
            "label": "Sleeveless High Neck Tank",
            "thumb": "clothing__sleeveless_high_neck_tank.jpg",
            "desc": "a structured sleeveless high-neck tank top with a clean crew / mock-neck rising close to the collarbone, smooth shoulder line, no straps, no detailing. Snug-but-not-tight fit through the upper torso. Hem hidden by the frame at the lower edge.",
        },
        "sleeveless_v_neck_shell": {
            "label": "Sleeveless V Neck Shell",
            "thumb": "clothing__sleeveless_v_neck_shell.jpg",
            "desc": "a tailored sleeveless shell top with a deep sharp V-neckline cut from the shoulders down toward the center chest, crisp tailored edges along both sides of the V — sleek vest / shell-top silhouette. Structured tailoring with clean shoulder lines.",
        },
        "tailored_blazer_sleeves_pushed": {
            "label": "Tailored Blazer Sleeves Pushed",
            "thumb": "clothing__tailored_blazer_sleeves_pushed.jpg",
            "desc": "an oversized tailored blazer with structured shoulders and a wide notch lapel, sleeves pushed or rolled up to mid-forearm to fully expose the wrist (jewelry hero zone), front draped open showing some of the upper chest skin. The blazer drapes off the shoulders cleanly without pulling.",
        },
    },
    "CLOTHING_MATERIAL": {
        "deep_black_ribbed_jersey": {
            "label": "Deep Black Ribbed Jersey",
            "thumb": "clothing_material__deep_black_ribbed_jersey.jpg",
            "desc": "deep saturated black ribbed knit jersey — fine vertical rib texture visible across the body of the garment, soft matte cotton-blend hand. The rib must read as real knit ribbing with soft tonal variation between ribs, NOT a tiled CG normal-map pattern, NOT a regular geometric grid. Strong contrast against the model's skin.",
        },
        "deep_black_wool_suiting": {
            "label": "Deep Black Wool Suiting",
            "thumb": "clothing_material__deep_black_wool_suiting.jpg",
            "desc": "deep saturated black tailored wool / wool-blend suiting fabric with a SUBTLE soft natural weave texture only. Matte to soft satin finish (NOT shiny, NOT lacquered, NOT plastic). The weave must read as real fine wool suiting with soft random fiber variation, NOT a pronounced cross-hatch, NOT a tiled CG normal-map pattern, NOT a regular geometric grid. Strong contrast against the model's skin.",
        },
        "warm_cocoa_brown_wool_suiting": {
            "label": "Warm Cocoa Brown Wool Suiting",
            "thumb": "clothing_material__warm_cocoa_brown_wool_suiting.jpg",
            "desc": "warm cocoa-brown tailored wool suiting fabric with a SUBTLE soft natural weave texture only. Matte finish, soft drape. The weave must read as real fine wool with soft random fiber variation, NOT a pronounced cross-hatch, NOT a tiled CG normal-map pattern. Strong warm contrast against fair skin, minor contrast against deeper skin.",
        },
        "ivory_off_white_silk_crepe": {
            "label": "Ivory Off-White Silk Crepe",
            "thumb": "clothing_material__ivory_off_white_silk_crepe.jpg",
            "desc": "soft ivory / off-white silk crepe fabric with fluid soft drape, subtle matte-to-soft-satin sheen, fine grainy crepe texture. NOT shiny-plastic, NOT lacquered, NOT a CG fabric pattern. The ivory must read clearly distinct from skin — minor contrast with fair skin, soft contrast against deeper skin.",
        },
    },
}

# First option of each parameter is the default fallback (used when a selection is
# missing or invalid). Auto-prepare randomizes instead of using these.
DEFAULTS: dict[str, str] = {p: next(iter(OPTIONS[p])) for p in PARAMS}


def option_keys(param: str) -> list[str]:
    return list(OPTIONS.get(param, {}).keys())


def label(param: str, key: str) -> str:
    """Display label for an option key (falls back to a prettified key)."""
    opt = OPTIONS.get(param, {}).get(key)
    if opt:
        return opt["label"]
    return key.replace("_", " ").title()


def thumb_filename(param: str, key: str) -> str | None:
    opt = OPTIONS.get(param, {}).get(key)
    return opt["thumb"] if opt else None


def normalize(params: dict | None) -> dict[str, str]:
    """Return a complete, valid selection dict — fill missing/invalid keys with defaults."""
    params = params or {}
    out: dict[str, str] = {}
    for p in PARAMS:
        v = params.get(p)
        out[p] = v if (v in OPTIONS[p]) else DEFAULTS[p]
    return out


def random_params(rng: random.Random | None = None) -> dict[str, str]:
    """A fresh randomized selection — one random option per parameter. Used by
    auto-prepare so every worn photo starts from a different styling combination."""
    r = rng or random
    return {p: r.choice(option_keys(p)) for p in PARAMS}


def assemble(template: str, params: dict | None) -> str:
    """Fill the six {PLACEHOLDER}s in the analyzer's templated prompt from `params`.

    Pure string replacement — the template already contains the literal joining
    words ([Hair] is "{HAIR_STYLE} {HAIR_COLOR}", [Wardrobe] is "{CLOTHING} in
    {CLOTHING_MATERIAL}"). Missing/invalid selections fall back to defaults."""
    sel = normalize(params)
    filled = template
    for p in PARAMS:
        desc = OPTIONS[p][sel[p]]["desc"]
        filled = filled.replace("{" + p + "}", desc)
    return filled


THUMBS_DIRNAME = "worn_thumbnails"


def thumbs_dir(prompts_root: Path) -> Path:
    return Path(prompts_root) / THUMBS_DIRNAME
