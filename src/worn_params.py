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
import re
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
            "desc": "fair Caucasian skin with a soft warm peachy undertone, healthy and luminous, with natural visible pores and a soft glow",
        },
        "medium_deep_international_brown": {
            "label": "Medium-Deep International Brown",
            "thumb": "skin_tone__medium_deep_international_brown.jpg",
            "desc": "medium-deep brown skin with a warm golden-bronze undertone, luminous and even, with natural visible pores and a soft golden glow",
        },
        "fair_pale_pink_blush": {
            "label": "Fair Pale with Pink Blush",
            "thumb": "skin_tone__fair_pale_pink_blush.jpg",
            "desc": "pale fair skin with a soft cool undertone and a gentle natural pink flush on the cheeks, luminous with natural visible pores",
        },
    },
    "MAKEUP": {
        "editorial_bronze_glow": {
            "label": "Editorial Bronze Glow",
            "thumb": "makeup__editorial_bronze_glow.jpg",
            "desc": "editorial bronze-glow beauty — soft pink lips with a subtle gloss, softly bronzed cheeks, natural defined brows, and a glowing radiant complexion",
        },
        "glossy_lip_beauty": {
            "label": "Glossy Lip Beauty",
            "thumb": "makeup__glossy_lip_beauty.jpg",
            "desc": "clean glossy-lip beauty — full hydrated soft pink lips with a soft dewy gloss, subtle peachy cheeks, natural brows, and luminous skin",
        },
    },
    "HAIR_STYLE": {
        "low_ponytail_natural_textured": {
            "label": "Low Ponytail Natural Textured",
            "thumb": "hair_style__low_ponytail_natural_textured.jpg",
            "desc": "in a low ponytail at the nape with natural texture and soft baby hairs at the hairline, the ears and neckline left fully clear",
        },
        "slicked_back_sleek": {
            "label": "Slicked Back Sleek",
            "thumb": "hair_style__slicked_back_sleek.jpg",
            "desc": "slicked back sleek and glossy with a clean editorial finish, the ears and neckline left fully clear, with soft baby hairs at the hairline",
        },
        "down_and_back": {
            "label": "Down And Back",
            "thumb": "hair_style__down_and_back.jpg",
            "desc": "worn down behind the shoulders, smooth and natural with soft sheen, the front of the neck, ears and neckline left fully clear",
        },
    },
    "HAIR_COLOR": {
        "natural_black": {
            "label": "Natural Black",
            "thumb": "hair_color__natural_black.jpg",
            "desc": "deep natural black, glossy with soft highlights",
        },
        "dark_brown": {
            "label": "Dark Brown",
            "thumb": "hair_color__dark_brown.jpg",
            "desc": "natural dark brown, glossy with visible strand detail",
        },
    },
    "CLOTHING": {
        "sleeveless_high_neck_tank": {
            "label": "Sleeveless High Neck Tank",
            "thumb": "clothing__sleeveless_high_neck_tank.jpg",
            "desc": "a structured sleeveless high-neck tank with a clean mock-neck and a smooth shoulder line",
        },
        "sleeveless_v_neck_shell": {
            "label": "Sleeveless V Neck Shell",
            "thumb": "clothing__sleeveless_v_neck_shell.jpg",
            "desc": "a tailored sleeveless shell top with a deep sharp V-neckline and clean shoulder lines",
        },
        "tailored_blazer_sleeves_pushed": {
            "label": "Tailored Blazer Sleeves Pushed",
            "thumb": "clothing__tailored_blazer_sleeves_pushed.jpg",
            "desc": "an oversized tailored blazer draped open off the shoulders, sleeves pushed to mid-forearm to expose the wrist",
        },
    },
    "CLOTHING_MATERIAL": {
        "deep_black_ribbed_jersey": {
            "label": "Deep Black Ribbed Jersey",
            "thumb": "clothing_material__deep_black_ribbed_jersey.jpg",
            "desc": "deep black ribbed knit jersey with a fine vertical rib and a soft matte cotton hand",
        },
        "deep_black_wool_suiting": {
            "label": "Deep Black Wool Suiting",
            "thumb": "clothing_material__deep_black_wool_suiting.jpg",
            "desc": "deep black wool suiting with a subtle natural weave and a soft matte finish",
        },
        "warm_cocoa_brown_wool_suiting": {
            "label": "Warm Cocoa Brown Wool Suiting",
            "thumb": "clothing_material__warm_cocoa_brown_wool_suiting.jpg",
            "desc": "warm cocoa-brown wool suiting with a subtle natural weave and a soft matte drape",
        },
        "ivory_off_white_silk_crepe": {
            "label": "Ivory Off-White Silk Crepe",
            "thumb": "clothing_material__ivory_off_white_silk_crepe.jpg",
            "desc": "soft ivory silk crepe with a fluid drape and a subtle satin sheen",
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

    Pure string replacement — the template joins them as "Hair: {HAIR_STYLE},
    {HAIR_COLOR}" and "{CLOTHING} in {CLOTHING_MATERIAL}". Missing/invalid
    selections fall back to defaults. As a safety net, any stray brace-token the
    analyzer may have echoed from its system prompt (e.g. a literal "{PLACEHOLDERS}")
    is stripped, so nothing un-filled ever reaches Gemini."""
    sel = normalize(params)
    filled = template
    for p in PARAMS:
        desc = OPTIONS[p][sel[p]]["desc"]
        filled = filled.replace("{" + p + "}", desc)
    filled = re.sub(r"\{[A-Z_]{2,}\}", "", filled)  # drop any leftover meta brace-token
    return filled


THUMBS_DIRNAME = "worn_thumbnails"


def thumbs_dir(prompts_root: Path) -> Path:
    return Path(prompts_root) / THUMBS_DIRNAME
