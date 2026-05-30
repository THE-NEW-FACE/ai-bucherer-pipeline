"""
Jewelry Color Harmonizer — Streamlit UI with subject-aware masking, per-image overrides,
board view, and size presets.

Launch via run_harmonizer.bat (Windows) or:
    streamlit run harmonizer.py
"""

import io
import zipfile
from dataclasses import dataclass, asdict

import numpy as np
import streamlit as st
from PIL import Image, ImageFilter, ImageOps


# ═══ Color space conversions (vectorized) ═══════════════════════════════

def srgb_to_linear(rgb_uint8: np.ndarray) -> np.ndarray:
    rgb = rgb_uint8.astype(np.float64) / 255.0
    return np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)


def linear_to_srgb(rgb_linear: np.ndarray) -> np.ndarray:
    rgb_linear = np.clip(rgb_linear, 0.0, 1.0)
    out = np.where(
        rgb_linear <= 0.0031308,
        12.92 * rgb_linear,
        1.055 * np.power(rgb_linear, 1 / 2.4) - 0.055,
    )
    return np.clip(out * 255.0, 0, 255).astype(np.uint8)


_M_RGB_TO_XYZ = np.array(
    [
        [0.4124564, 0.3575761, 0.1804375],
        [0.2126729, 0.7151522, 0.0721750],
        [0.0193339, 0.1191920, 0.9503041],
    ]
)
_M_XYZ_TO_RGB = np.array(
    [
        [3.2404542, -1.5371385, -0.4985314],
        [-0.9692660, 1.8760108, 0.0415560],
        [0.0556434, -0.2040259, 1.0572252],
    ]
)
_WHITE = np.array([0.95047, 1.0, 1.08883])
_DELTA = 6.0 / 29.0


def _f_forward(t: np.ndarray) -> np.ndarray:
    return np.where(t > _DELTA ** 3, np.cbrt(np.clip(t, 0, None)), t / (3 * _DELTA ** 2) + 4 / 29)


def _f_inverse(t: np.ndarray) -> np.ndarray:
    return np.where(t > _DELTA, t ** 3, 3 * _DELTA ** 2 * (t - 4 / 29))


def rgb_to_lab(rgb_uint8: np.ndarray) -> np.ndarray:
    lin = srgb_to_linear(rgb_uint8)
    xyz = lin @ _M_RGB_TO_XYZ.T
    f = _f_forward(xyz / _WHITE)
    L = 116 * f[..., 1] - 16
    a = 500 * (f[..., 0] - f[..., 1])
    b = 200 * (f[..., 1] - f[..., 2])
    return np.stack([L, a, b], axis=-1)


def lab_to_rgb(lab: np.ndarray) -> np.ndarray:
    L, a, b = lab[..., 0], lab[..., 1], lab[..., 2]
    fy = (L + 16) / 116
    fx = a / 500 + fy
    fz = fy - b / 200
    xyz = np.stack([_f_inverse(fx), _f_inverse(fy), _f_inverse(fz)], axis=-1) * _WHITE
    lin = xyz @ _M_XYZ_TO_RGB.T
    return linear_to_srgb(lin)


def hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def hex_to_lab(h: str) -> np.ndarray:
    r, g, b = hex_to_rgb(h)
    arr = np.array([[[r, g, b]]], dtype=np.uint8)
    return rgb_to_lab(arr)[0, 0]


# ═══ Subject / background masking ════════════════════════════════════════

def _background_components(candidate: np.ndarray, min_area_frac: float = 0.004) -> np.ndarray:
    """From a binary 'looks like backdrop' mask, keep only true background regions:
    components that TOUCH THE BORDER (open seamless backdrop) OR are large enough to be
    enclosed backdrop (e.g. the hole inside a hoop). This drops small interior bright
    specks — gold speculars and near-white highlights — that share the backdrop's
    brightness/low-chroma but are surrounded by jewellery, which is exactly what was
    getting wrongly whitened. Degrades to a no-op if scipy is unavailable."""
    try:
        from scipy import ndimage
    except Exception:
        return candidate
    lbl, n = ndimage.label(candidate)
    if n == 0:
        return candidate
    h, w = candidate.shape
    min_area = max(64, int(min_area_frac * h * w))
    border = np.unique(np.concatenate([lbl[0, :], lbl[-1, :], lbl[:, 0], lbl[:, -1]]))
    keep = set(int(i) for i in border if i != 0)
    sizes = ndimage.sum(np.ones_like(candidate, dtype=np.float32), lbl,
                        index=np.arange(1, n + 1))
    for i in range(1, n + 1):
        if sizes[i - 1] >= min_area:
            keep.add(i)
    if not keep:
        return np.zeros_like(candidate)
    return np.isin(lbl, list(keep))


def detect_background_mask(
    lab: np.ndarray,
    l_threshold: float = 88.0,
    chroma_threshold: float = 12.0,
    ramp: float = 6.0,
    blur_radius: float = 2.0,
) -> np.ndarray:
    """
    Background mask = (L is bright enough) AND (chroma is below threshold), then RESTRICTED
    to border-connected / large regions so interior gold speculars don't leak in.
    Chroma is a HARD gate (binary), L uses a soft ramp; the spatial restriction is what
    keeps bright jewellery highlights out of the background. Gaussian-blur for clean edges.
    """
    L = lab[..., 0]
    chroma = np.sqrt(lab[..., 1] ** 2 + lab[..., 2] ** 2)
    l_score = np.clip((L - l_threshold) / ramp, 0.0, 1.0)
    chroma_gate = (chroma < chroma_threshold).astype(np.float64)
    # Spatially confine the candidate to the actual backdrop (border-connected or large
    # enclosed), so a gold highlight that happens to be bright + low-chroma is excluded.
    candidate = (l_score > 0.0) & (chroma_gate > 0.5)
    candidate = _background_components(candidate)
    mask = l_score * chroma_gate * candidate.astype(np.float64)
    if blur_radius > 0:
        mask_img = Image.fromarray((mask * 255).astype(np.uint8), mode="L")
        mask_img = mask_img.filter(ImageFilter.GaussianBlur(radius=blur_radius))
        mask = np.array(mask_img, dtype=np.float64) / 255.0
    return mask


# ═══ Color transfer ══════════════════════════════════════════════════════

def reinhard_transfer_masked(
    hero_lab: np.ndarray,
    target_lab: np.ndarray,
    hero_fg_mask: np.ndarray,
    target_fg_mask: np.ndarray,
    min_pixels: int = 500,
) -> np.ndarray:
    hero_fg = hero_lab[hero_fg_mask > 0.5]
    target_fg = target_lab[target_fg_mask > 0.5]
    if hero_fg.shape[0] < min_pixels:
        hero_fg = hero_lab.reshape(-1, 3)
    if target_fg.shape[0] < min_pixels:
        target_fg = target_lab.reshape(-1, 3)
    result = np.empty_like(target_lab)
    for c in range(3):
        h_mean = float(hero_fg[..., c].mean())
        h_std = float(hero_fg[..., c].std())
        t_mean = float(target_fg[..., c].mean())
        t_std = max(float(target_fg[..., c].std()), 1e-6)
        result[..., c] = (target_lab[..., c] - t_mean) * (h_std / t_std) + h_mean
    return result


def normalize_background(
    lab: np.ndarray,
    bg_mask: np.ndarray,
    bg_lab_lo: np.ndarray,
    bg_lab_hi: np.ndarray,
    l_threshold: float = 88.0,
) -> np.ndarray:
    """
    Map background pixels into the [bg_lab_lo, bg_lab_hi] range using an ABSOLUTE
    luminance → range-position mapping. The same source L always maps to the same output
    color, irrespective of the per-image background statistics — this is what makes the
    batch land consistently inside the target range.

    L blends softly (preserve subtle backdrop gradients/shadows).
    a, b use a sharpened mask weight so any residual original tint is fully overridden
    where the mask is significant.
    """
    if bg_mask.max() < 0.05:
        return lab

    # Absolute L → position in range. l_threshold = range floor, 100 = range ceiling.
    L_pos = np.clip((lab[..., 0] - l_threshold) / max(100.0 - l_threshold, 1.0), 0.0, 1.0)
    new_L = bg_lab_lo[0] + L_pos * (bg_lab_hi[0] - bg_lab_lo[0])
    new_a = bg_lab_lo[1] + L_pos * (bg_lab_hi[1] - bg_lab_lo[1])
    new_b = bg_lab_lo[2] + L_pos * (bg_lab_hi[2] - bg_lab_lo[2])

    # Stronger weight on color (a/b) — drives the tint to the target even at moderate mask
    color_mask = np.clip(bg_mask * 2.0, 0.0, 1.0)

    result = lab.copy()
    result[..., 0] = lab[..., 0] * (1 - bg_mask) + new_L * bg_mask
    result[..., 1] = lab[..., 1] * (1 - color_mask) + new_a * color_mask
    result[..., 2] = lab[..., 2] * (1 - color_mask) + new_b * color_mask
    return result


def estimate_backdrop_lab(lab: np.ndarray, l_pct: float = 80.0,
                          chroma_cap: float = 25.0) -> np.ndarray:
    """Estimate an image's white seamless backdrop colour as a mean (L, a, b).

    On a packshot the backdrop is the large, brightest, near-neutral region; gold is
    darker and far more chromatic. We take the brightest pixels (top (100-l_pct)%) that
    are still reasonably neutral (chroma ≤ cap, to reject bright gold speculars). The
    mean (a, b) is the illuminant cast (≈0 for true white, positive b for cream); the
    mean L is the backdrop brightness. Used both to neutralize the cast and — when
    matching the reference — to fill every shot's background to the SAME exact colour."""
    L = lab[..., 0]
    chroma = np.sqrt(lab[..., 1] ** 2 + lab[..., 2] ** 2)
    thr = np.percentile(L, l_pct)
    sel = (L >= thr) & (chroma <= chroma_cap)
    if int(sel.sum()) < 500:
        sel = L >= thr                                  # relax the chroma cap
    if int(sel.sum()) < 500:
        sel = L >= np.percentile(L, 50.0)               # last resort
    return np.array([float(lab[..., 0][sel].mean()),
                     float(lab[..., 1][sel].mean()),
                     float(lab[..., 2][sel].mean())])


def neutralize_cast(lab: np.ndarray, a_off: float, b_off: float) -> np.ndarray:
    """Remove a global illuminant cast by shifting a/b so the backdrop becomes neutral.
    Applied to the whole image (it's an illuminant correction): the backdrop lands on
    a≈b≈0 and the subject's own cast is corrected the same way, so every image in a set
    is brought to the same white point before matching — the key to real convergence."""
    out = lab.copy()
    out[..., 1] = lab[..., 1] - a_off
    out[..., 2] = lab[..., 2] - b_off
    return out


@dataclass
class Settings:
    """All per-image tunable parameters. Hashable for caching."""
    strength: float = 0.7
    preserve_luminance: bool = False
    use_subject_mask: bool = True
    bg_normalize: bool = True
    bg_l_threshold: float = 88.0
    bg_chroma_threshold: float = 12.0
    bg_color_lo_hex: str = "F4F4F5"
    bg_color_hi_hex: str = "F9FAFF"
    bg_warmth: float = 0.0
    gold_sat: float = 0.0
    diamond_cool: float = 0.0
    # White-balance neutralization: remove each image's backdrop illuminant cast before
    # masking/transfer so warm/cream backgrounds converge to a common white. Packshot-only
    # (off for worn — a global a/b shift would distort skin/lifestyle pixels).
    wb_neutralize: bool = False
    # Fill the background to the reference's exact (neutralized) backdrop colour instead
    # of the hex range, so every shot in a set gets an IDENTICAL background.
    bg_match_hero: bool = False


def harmonize(
    hero_rgb: np.ndarray,
    target_rgb: np.ndarray,
    s: Settings,
) -> tuple[np.ndarray, np.ndarray]:
    hero_lab = rgb_to_lab(hero_rgb)
    target_lab = rgb_to_lab(target_rgb)

    # White-balance: neutralize each image's backdrop cast FIRST, so a warm/cream
    # background (which the chroma gate would otherwise misclassify as foreground and
    # leave un-whitened, while polluting the colour-transfer stats) is corrected on both
    # hero and target before anything else. This is what makes the set truly converge.
    if getattr(s, "wb_neutralize", False):
        t_bd = estimate_backdrop_lab(target_lab)
        target_lab = neutralize_cast(target_lab, t_bd[1], t_bd[2])
        h_bd = estimate_backdrop_lab(hero_lab)
        hero_lab = neutralize_cast(hero_lab, h_bd[1], h_bd[2])

    if s.use_subject_mask:
        hero_bg = detect_background_mask(hero_lab, s.bg_l_threshold, s.bg_chroma_threshold)
        target_bg = detect_background_mask(target_lab, s.bg_l_threshold, s.bg_chroma_threshold)
    else:
        hero_bg = np.zeros(hero_lab.shape[:2])
        target_bg = np.zeros(target_lab.shape[:2])

    hero_fg = 1.0 - hero_bg
    target_fg = 1.0 - target_bg

    matched = reinhard_transfer_masked(hero_lab, target_lab, hero_fg, target_fg)
    if s.preserve_luminance:
        matched[..., 0] = target_lab[..., 0]

    # Apply Reinhard color transfer only to foreground pixels.
    # Keeping BG pixels untouched here means normalize_background() sees the original BG
    # values, which gives the absolute L→range mapping a consistent input across images.
    if s.use_subject_mask:
        fg_strength = (s.strength * target_fg)[..., None]
        blended = target_lab * (1.0 - fg_strength) + matched * fg_strength
    else:
        blended = target_lab * (1.0 - s.strength) + matched * s.strength

    if s.bg_normalize and s.use_subject_mask:
        if getattr(s, "bg_match_hero", False):
            # Fill every shot's background to the SAME exact colour — the reference's
            # backdrop (neutralized) — so backgrounds are identical across the set, not
            # merely mapped into a range. lo==hi collapses the L→range ramp to a flat fill.
            hb = estimate_backdrop_lab(hero_lab)
            bg_lo = bg_hi = hb
        else:
            bg_lo = hex_to_lab(s.bg_color_lo_hex)
            bg_hi = hex_to_lab(s.bg_color_hi_hex)
            if bg_lo[0] > bg_hi[0]:
                bg_lo, bg_hi = bg_hi, bg_lo
        blended = normalize_background(blended, target_bg, bg_lo, bg_hi, s.bg_l_threshold)

    if s.bg_warmth != 0 and s.use_subject_mask:
        blended[..., 2] = blended[..., 2] + s.bg_warmth * target_bg

    if s.gold_sat != 0:
        b_chan = blended[..., 2]
        a_chan = blended[..., 1]
        product_mask = 1.0 - target_bg if s.use_subject_mask else np.ones_like(b_chan)
        gold_zone = ((b_chan > 12) & (a_chan > -5)).astype(np.float64) * product_mask
        scale = 1.0 + s.gold_sat / 100.0
        blended[..., 1] = blended[..., 1] * (1 + gold_zone * (scale - 1))
        blended[..., 2] = blended[..., 2] * (1 + gold_zone * (scale - 1))

    if s.diamond_cool != 0:
        L = blended[..., 0]
        product_mask = 1.0 - target_bg if s.use_subject_mask else np.ones_like(L)
        hl_strength = np.clip((L - 80) / 15, 0, 1) * product_mask
        blended[..., 2] = blended[..., 2] - s.diamond_cool * hl_strength

    return lab_to_rgb(blended), target_bg


# ═══ Image helpers ═══════════════════════════════════════════════════════

def load_image(file) -> np.ndarray:
    img = Image.open(file)
    img = ImageOps.exif_transpose(img).convert("RGB")
    return np.array(img)


def fit(arr: np.ndarray, max_side: int) -> np.ndarray:
    h, w = arr.shape[:2]
    if max(h, w) <= max_side:
        return arr
    scale = max_side / max(h, w)
    new_w, new_h = int(round(w * scale)), int(round(h * scale))
    img = Image.fromarray(arr).resize((new_w, new_h), Image.LANCZOS)
    return np.array(img)


def mask_overlay(rgb: np.ndarray, mask: np.ndarray, color=(255, 0, 255), alpha=0.45) -> np.ndarray:
    overlay = np.array(color, dtype=np.float64)
    m = mask[..., None] * alpha
    result = rgb.astype(np.float64) * (1 - m) + overlay * m
    return np.clip(result, 0, 255).astype(np.uint8)


SIZE_PRESETS = {"Small": 220, "Medium": 380, "Large": 640, "Full": None}
BOARD_COLS = {"Small": 5, "Medium": 4, "Large": 3, "Full": 2}
# Preview compute size — chosen to comfortably exceed display CSS pixels even on HiDPI
# (2× CSS px for retina + headroom for board columns expanding in Full mode).
SIZE_PREVIEW_MAX = {"Small": 700, "Medium": 1100, "Large": 1600, "Full": 2200}


def render_image(arr, *, caption=None, width=None):
    """Streamlit image with backward-compat for old/new width API."""
    kwargs = {"caption": caption}
    if width is None:
        try:
            st.image(arr, width="stretch", **kwargs)
        except TypeError:
            st.image(arr, use_column_width=True, **kwargs)
    else:
        st.image(arr, width=width, **kwargs)


# ═══ Per-image settings resolution ═══════════════════════════════════════

OVERRIDE_KEY = lambda name: f"override_{name}"
ENABLED_KEY = lambda name: f"override_enabled_{name}"


def resolve_settings(filename: str, globals_: Settings) -> Settings:
    """Return the Settings for this image — overrides if enabled, else globals."""
    if not st.session_state.get(ENABLED_KEY(filename), False):
        return globals_
    ov = st.session_state.get(OVERRIDE_KEY(filename))
    if not ov:
        return globals_
    return Settings(**{**asdict(globals_), **ov})


def render_overrides_ui(filename: str, globals_: Settings) -> Settings:
    """Render the override controls under one image. Returns Settings to apply."""
    has_overrides = st.session_state.get(ENABLED_KEY(filename), False)
    badge = "  🔧 Custom" if has_overrides else ""

    with st.expander(f"⚙ Per-image settings{badge}", expanded=has_overrides):
        enabled = st.checkbox(
            "Override global settings for this image",
            value=has_overrides,
            key=ENABLED_KEY(filename),
        )

        if not enabled:
            return globals_

        # Seed override dict from globals if first time
        if OVERRIDE_KEY(filename) not in st.session_state:
            st.session_state[OVERRIDE_KEY(filename)] = asdict(globals_)

        ov = dict(st.session_state[OVERRIDE_KEY(filename)])

        c1, c2, c3 = st.columns(3)
        with c1:
            st.caption("**Match**")
            ov["strength"] = st.slider(
                "Strength", 0, 100, int(ov.get("strength", 0.7) * 100),
                key=f"ov_strength_{filename}",
            ) / 100.0
            ov["preserve_luminance"] = st.checkbox(
                "Preserve luminance", value=ov.get("preserve_luminance", False),
                key=f"ov_lum_{filename}",
            )
        with c2:
            st.caption("**Background**")
            ov["bg_normalize"] = st.checkbox(
                "Normalize", value=ov.get("bg_normalize", True),
                key=f"ov_bgnorm_{filename}",
            )
            ov["bg_color_lo_hex"] = st.color_picker(
                "BG low", value=f"#{ov.get('bg_color_lo_hex', 'F4F4F5')}",
                key=f"ov_bglo_{filename}",
            ).lstrip("#")
            ov["bg_color_hi_hex"] = st.color_picker(
                "BG high", value=f"#{ov.get('bg_color_hi_hex', 'F9FAFF')}",
                key=f"ov_bghi_{filename}",
            ).lstrip("#")
        with c3:
            st.caption("**Fine-tune**")
            ov["bg_warmth"] = st.slider(
                "BG warmth", -10, 10, int(ov.get("bg_warmth", 0)),
                key=f"ov_bgwarm_{filename}",
            )
            ov["gold_sat"] = st.slider(
                "Gold sat", -30, 30, int(ov.get("gold_sat", 0)),
                key=f"ov_gold_{filename}",
            )
            ov["diamond_cool"] = st.slider(
                "Diamond cool", -15, 15, int(ov.get("diamond_cool", 0)),
                key=f"ov_dia_{filename}",
            )

        # Advanced mask thresholds
        with st.expander("Mask detection (advanced)", expanded=False):
            mc1, mc2 = st.columns(2)
            ov["bg_l_threshold"] = mc1.slider(
                "BG L threshold", 70, 95, int(ov.get("bg_l_threshold", 88)),
                key=f"ov_bgL_{filename}",
            )
            ov["bg_chroma_threshold"] = mc2.slider(
                "BG chroma threshold", 4, 25, int(ov.get("bg_chroma_threshold", 12)),
                key=f"ov_bgC_{filename}",
            )
            ov["use_subject_mask"] = st.checkbox(
                "Use subject-aware masking", value=ov.get("use_subject_mask", True),
                key=f"ov_mask_{filename}",
            )

        if st.button("↺ Reset to global", key=f"ov_reset_{filename}"):
            for k in list(st.session_state.keys()):
                if k.endswith(f"_{filename}") and k.startswith("ov_"):
                    del st.session_state[k]
            if OVERRIDE_KEY(filename) in st.session_state:
                del st.session_state[OVERRIDE_KEY(filename)]
            if ENABLED_KEY(filename) in st.session_state:
                del st.session_state[ENABLED_KEY(filename)]
            st.rerun()

        st.session_state[OVERRIDE_KEY(filename)] = ov
        return Settings(**ov)


# ═══ Sidebar (global settings) ═══════════════════════════════════════════

def render_sidebar() -> tuple[Settings, dict]:
    """Returns (globals_settings, ui_state) where ui_state has hero/targets/show_mask/export."""
    with st.sidebar:
        st.header("1 · Hero (reference)")
        hero_file = st.file_uploader(
            "Drop your hero image",
            type=["png", "jpg", "jpeg", "tif", "tiff", "webp"],
        )

        st.header("2 · Targets")
        target_files = st.file_uploader(
            "Drop images to harmonize",
            type=["png", "jpg", "jpeg", "tif", "tiff", "webp"],
            accept_multiple_files=True,
        )

        st.header("3 · Background range")
        use_subject_mask = st.checkbox(
            "Subject-aware masking",
            value=True,
            help="Detect background separately from product. Color transfer only on product pixels.",
        )
        bg_normalize = st.checkbox(
            "Normalize background to range",
            value=True,
            disabled=not use_subject_mask,
        )
        bg_lo = st.color_picker(
            "Background low (shadows / cool corners)", value="#F4F4F5",
            disabled=not (use_subject_mask and bg_normalize),
        )
        bg_hi = st.color_picker(
            "Background high (brightest backdrop)", value="#F9FAFF",
            disabled=not (use_subject_mask and bg_normalize),
        )

        with st.expander("Mask detection (advanced)", expanded=False):
            bg_l_threshold = st.slider(
                "BG L threshold", 70, 95, 88,
                disabled=not use_subject_mask,
                help="Pixels below this lightness can't be background.",
            )
            bg_chroma_threshold = st.slider(
                "BG chroma threshold", 4, 25, 12,
                disabled=not use_subject_mask,
                help="Pixels above this chroma can't be background.",
            )
            show_mask = st.checkbox(
                "Show detected background as magenta overlay",
                value=False,
                disabled=not use_subject_mask,
            )

        st.header("4 · Match strength")
        strength = st.slider(
            "Color match strength", 0, 100, 70,
            help="How strongly to pull target product colors toward the hero.",
        ) / 100.0
        preserve_lum = st.checkbox(
            "Preserve target luminance", value=False,
            help="Transfer only color, keep target's brightness.",
        )

        with st.expander("Fine-tune", expanded=False):
            bg_warmth = st.slider("Background warmth", -10, 10, 0)
            gold_sat = st.slider("Gold saturation", -30, 30, 0)
            diamond_cool = st.slider("Diamond coolness", -15, 15, 0)

        st.header("5 · Export")
        output_format = st.selectbox("Output format", ["Same as input", "PNG", "JPEG", "WEBP"], index=0)
        output_quality = st.select_slider("JPEG / WEBP quality", options=[70, 80, 85, 90, 95, 100], value=95)

    globals_ = Settings(
        strength=strength,
        preserve_luminance=preserve_lum,
        use_subject_mask=use_subject_mask,
        bg_normalize=bg_normalize,
        bg_l_threshold=bg_l_threshold,
        bg_chroma_threshold=bg_chroma_threshold,
        bg_color_lo_hex=bg_lo.lstrip("#"),
        bg_color_hi_hex=bg_hi.lstrip("#"),
        bg_warmth=bg_warmth,
        gold_sat=gold_sat,
        diamond_cool=diamond_cool,
    )
    ui_state = {
        "hero_file": hero_file,
        "target_files": target_files,
        "show_mask": show_mask,
        "output_format": output_format,
        "output_quality": output_quality,
    }
    return globals_, ui_state


# ═══ Top control bar ═════════════════════════════════════════════════════

def render_top_bar(n_targets: int) -> tuple[str, str]:
    """Renders the view-mode + size selector. Returns (view_mode, image_size)."""
    # Initialize state on first run
    if "view_mode" not in st.session_state:
        st.session_state.view_mode = "Detail"
    if "image_size" not in st.session_state:
        st.session_state.image_size = "Medium"
    if "focused_target" not in st.session_state:
        st.session_state.focused_target = None

    bar = st.container()
    with bar:
        c1, c2, c3 = st.columns([2, 3, 2])
        with c1:
            view = st.radio(
                "View", ["Detail", "Board"],
                horizontal=True,
                key="view_mode",
                label_visibility="collapsed",
            )
        with c2:
            size = st.radio(
                "Image size", list(SIZE_PRESETS.keys()),
                horizontal=True,
                key="image_size",
                label_visibility="collapsed",
            )
        with c3:
            if st.session_state.focused_target:
                if st.button(f"← Show all ({n_targets})", use_container_width=True):
                    st.session_state.focused_target = None
                    st.rerun()
            else:
                st.caption(f"{n_targets} image{'s' if n_targets != 1 else ''} loaded")
    return view, size


# ═══ Rendering: Detail and Board views ═══════════════════════════════════

def render_detail_card(tf, hero_arr, globals_settings, show_mask, image_size):
    """One target rendered in Detail view: before/after + per-image overrides."""
    target_arr = load_image(tf)
    settings_for_image = resolve_settings(tf.name, globals_settings)

    # Scale preview resolution to the chosen display size — avoids browser upscaling blur.
    preview_max = SIZE_PREVIEW_MAX[image_size]
    hero_preview = fit(hero_arr, preview_max)
    target_preview = fit(target_arr, preview_max)
    result_arr, mask = harmonize(hero_preview, target_preview, settings_for_image)

    is_custom = st.session_state.get(ENABLED_KEY(tf.name), False)
    badge = "  🔧" if is_custom else ""

    with st.container(border=True):
        head_l, head_r = st.columns([4, 1])
        head_l.markdown(f"**{tf.name}**{badge}  ·  {target_arr.shape[1]}×{target_arr.shape[0]}")
        if not st.session_state.focused_target and head_r.button(
            "Focus", key=f"focus_{tf.name}", use_container_width=True,
            help="Show only this image",
        ):
            st.session_state.focused_target = tf.name
            st.rerun()

        width = SIZE_PRESETS[image_size]
        c1, c2 = st.columns(2)
        with c1:
            render_image(result_arr if False else target_preview, caption="Before", width=width)
        with c2:
            render_image(result_arr, caption="After", width=width)

        if globals_settings.use_subject_mask and show_mask:
            render_image(
                mask_overlay(target_preview, mask),
                caption=f"Detected background — {float(mask.mean() * 100):.1f}% of frame",
                width=width,
            )

        render_overrides_ui(tf.name, globals_settings)


def render_detail_view(target_files, hero_arr, globals_settings, show_mask, image_size):
    """Detail = vertical list of before/after cards. Single target if focused."""
    focused = st.session_state.focused_target
    if focused:
        match = next((tf for tf in target_files if tf.name == focused), None)
        if match:
            render_detail_card(match, hero_arr, globals_settings, show_mask, image_size)
            return
        # focused image was removed; clear
        st.session_state.focused_target = None

    for tf in target_files:
        render_detail_card(tf, hero_arr, globals_settings, show_mask, image_size)


def render_board_view(target_files, hero_arr, globals_settings, image_size):
    """Board = grid of After thumbnails. Click → switch to Detail focused on that image."""
    n_cols = BOARD_COLS[image_size]
    width = SIZE_PRESETS[image_size]
    preview_max = SIZE_PREVIEW_MAX[image_size]
    hero_preview = fit(hero_arr, preview_max)

    rows = [target_files[i:i + n_cols] for i in range(0, len(target_files), n_cols)]
    for row in rows:
        cols = st.columns(n_cols)
        for col, tf in zip(cols, row):
            with col:
                target_arr = load_image(tf)
                target_preview = fit(target_arr, preview_max)
                s = resolve_settings(tf.name, globals_settings)
                result_arr, _ = harmonize(hero_preview, target_preview, s)
                is_custom = st.session_state.get(ENABLED_KEY(tf.name), False)
                badge = "🔧 " if is_custom else ""
                render_image(result_arr, caption=f"{badge}{tf.name}", width=width)
                if st.button("✏ Edit", key=f"board_edit_{tf.name}", use_container_width=True):
                    st.session_state.focused_target = tf.name
                    st.session_state.view_mode = "Detail"
                    st.rerun()


# ═══ Export ══════════════════════════════════════════════════════════════

def build_zip(results: list[tuple[str, Image.Image]], output_format: str, output_quality: int) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, img in results:
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else "png"
            stem = name.rsplit(".", 1)[0] if "." in name else name
            if output_format == "PNG":
                fmt, ext_out, opts = "PNG", "png", {}
            elif output_format == "JPEG":
                fmt, ext_out, opts = "JPEG", "jpg", {"quality": output_quality}
            elif output_format == "WEBP":
                fmt, ext_out, opts = "WEBP", "webp", {"quality": output_quality}
            else:
                if ext in ("jpg", "jpeg"):
                    fmt, ext_out, opts = "JPEG", "jpg", {"quality": output_quality}
                elif ext == "webp":
                    fmt, ext_out, opts = "WEBP", "webp", {"quality": output_quality}
                elif ext in ("tif", "tiff"):
                    fmt, ext_out, opts = "TIFF", "tif", {}
                else:
                    fmt, ext_out, opts = "PNG", "png", {}
            img_buf = io.BytesIO()
            img.save(img_buf, format=fmt, **opts)
            zf.writestr(f"harmonized_{stem}.{ext_out}", img_buf.getvalue())
    buf.seek(0)
    return buf.getvalue()


def render_export(target_files, hero_arr, globals_settings, output_format, output_quality):
    st.subheader("Export")
    st.caption(
        "Click below to render all images at **full resolution** with their current settings "
        "(including per-image overrides), then download as ZIP."
    )
    if st.button("📦 Build harmonized ZIP", type="primary"):
        with st.spinner("Rendering full-resolution outputs…"):
            results: list[tuple[str, Image.Image]] = []
            for tf in target_files:
                target_arr = load_image(tf)
                s = resolve_settings(tf.name, globals_settings)
                result_arr, _ = harmonize(hero_arr, target_arr, s)
                results.append((tf.name, Image.fromarray(result_arr)))
            zip_bytes = build_zip(results, output_format, output_quality)
        st.download_button(
            "⬇️ Download harmonized.zip",
            data=zip_bytes,
            file_name="harmonized.zip",
            mime="application/zip",
        )
        st.success(f"Packaged {len(results)} harmonized images.")


# ═══ Main entry ══════════════════════════════════════════════════════════

st.set_page_config(layout="wide", page_title="Jewelry Color Harmonizer", page_icon="✨")
st.markdown(
    """
    <style>
    .block-container { padding-top: 2rem; }
    .stImage img { border-radius: 4px; }
    .swatch { display: inline-block; width: 18px; height: 18px; vertical-align: middle;
              border: 1px solid #ddd; border-radius: 3px; margin-right: 4px; }
    </style>
    """,
    unsafe_allow_html=True,
)
st.title("Jewelry Color Harmonizer")
st.caption("Subject-aware LAB transfer + background normalization + per-image overrides.")

globals_settings, ui = render_sidebar()
hero_file = ui["hero_file"]
target_files = ui["target_files"]
show_mask = ui["show_mask"]

if not hero_file:
    st.info("👈 Drop a hero image in the sidebar to start.")
    st.markdown(
        f"**Default background range:** "
        f"<span class='swatch' style='background:#{globals_settings.bg_color_lo_hex}'></span>`#{globals_settings.bg_color_lo_hex.upper()}` → "
        f"<span class='swatch' style='background:#{globals_settings.bg_color_hi_hex}'></span>`#{globals_settings.bg_color_hi_hex.upper()}`",
        unsafe_allow_html=True,
    )
    st.stop()

hero_arr = load_image(hero_file)

if not target_files:
    st.subheader("Hero")
    render_image(fit(hero_arr, 1200), caption=hero_file.name, width=500)
    if globals_settings.use_subject_mask:
        hero_lab = rgb_to_lab(hero_arr)
        hero_bg = detect_background_mask(
            hero_lab, globals_settings.bg_l_threshold, globals_settings.bg_chroma_threshold
        )
        st.caption(f"Hero background coverage: ~{float(hero_bg.mean() * 100):.1f}%")
    st.info("👈 Now drop one or more target images in the sidebar.")
    st.stop()

# ─── Top bar
view_mode, image_size = render_top_bar(len(target_files))

# ─── Hero preview (compact)
with st.container():
    hc1, hc2 = st.columns([1, 4])
    with hc1:
        render_image(fit(hero_arr, 360), caption=f"Hero: {hero_file.name}", width=200)
    with hc2:
        if globals_settings.use_subject_mask:
            hero_for_mask = fit(hero_arr, SIZE_PREVIEW_MAX[image_size])
            hero_lab = rgb_to_lab(hero_for_mask)
            hero_bg = detect_background_mask(
                hero_lab, globals_settings.bg_l_threshold, globals_settings.bg_chroma_threshold
            )
            st.markdown(
                f"**Background range:** "
                f"<span class='swatch' style='background:#{globals_settings.bg_color_lo_hex}'></span>`#{globals_settings.bg_color_lo_hex.upper()}` "
                f"→ <span class='swatch' style='background:#{globals_settings.bg_color_hi_hex}'></span>`#{globals_settings.bg_color_hi_hex.upper()}`  ·  "
                f"Hero BG ~{float(hero_bg.mean() * 100):.0f}% of frame",
                unsafe_allow_html=True,
            )
            if show_mask:
                render_image(
                    mask_overlay(hero_for_mask, hero_bg),
                    caption="Hero — detected background (magenta)",
                    width=SIZE_PRESETS[image_size],
                )

st.markdown("---")

# ─── Targets
header_label = (
    f"Targets ({len(target_files)})"
    if not st.session_state.focused_target
    else f"Editing: {st.session_state.focused_target}"
)
st.subheader(header_label)

with st.spinner("Harmonizing…"):
    if view_mode == "Board" and not st.session_state.focused_target:
        render_board_view(target_files, hero_arr, globals_settings, image_size)
    else:
        render_detail_view(target_files, hero_arr, globals_settings, show_mask, image_size)

st.markdown("---")
render_export(target_files, hero_arr, globals_settings, ui["output_format"], ui["output_quality"])
