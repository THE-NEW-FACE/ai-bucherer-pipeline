"""
Grader — calls the existing _pipeline/harmonizer.py headlessly with packshot/worn presets.

The harmonizer file runs Streamlit UI code at import time. We stub `streamlit` (and absorb
the `st.stop()` raised when no files are uploaded) to import the math functions cleanly.
This mirrors `_pipeline/_smoke_test.py`.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image

def _resolve_harmonizer_path() -> Path:
    """Locate harmonizer.py. Prefer the copy vendored inside the package (so the repo
    is self-contained on a cloud host); fall back to the original sibling `_pipeline/`
    folder for local development checkouts."""
    pkg_root = Path(__file__).resolve().parent.parent  # _auto_pipeline/
    candidates = [
        pkg_root / "_pipeline" / "harmonizer.py",        # vendored (deploy)
        pkg_root.parent / "_pipeline" / "harmonizer.py",  # sibling (local dev)
    ]
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]  # report the expected vendored path in the error


_HARMONIZER_PATH = _resolve_harmonizer_path()

# ── Streamlit stub — temporarily replaces real streamlit during harmonizer import ──
#
# The harmonizer module runs its Streamlit UI at top level. To import its math functions
# without rendering UI (or crashing because no files are uploaded), we swap in a stub for
# the duration of the import, then restore whatever was there before. This works whether
# we're inside a real Streamlit app (the app then resumes with the real module) or in a
# headless script (the real module was never loaded).

def _build_streamlit_stub() -> tuple[types.ModuleType, type]:
    fake = types.ModuleType("streamlit")

    class _NoOp:
        def __getattr__(self, _name): return _NoOp()
        def __call__(self, *a, **kw): return None
        def __bool__(self): return False
        def __enter__(self): return self
        def __exit__(self, *a): return False

    class _Halt(Exception):
        pass

    def _stop(*a, **kw):
        raise _Halt()

    fake.set_page_config = lambda **kw: None
    fake.markdown = lambda *a, **kw: None
    fake.title = fake.caption = fake.header = fake.subheader = lambda *a, **kw: None
    fake.info = fake.success = lambda *a, **kw: None
    fake.stop = _stop
    fake.file_uploader = lambda *a, **kw: None
    fake.color_picker = lambda label, value="#FFFFFF", **kw: value
    fake.checkbox = lambda *a, value=False, **kw: value
    fake.slider = lambda label, lo, hi, default, **kw: default
    fake.select_slider = lambda label, options, value, **kw: value
    fake.selectbox = lambda label, options, index=0, **kw: options[index]
    fake.radio = lambda label, options, **kw: options[0]
    fake.button = lambda *a, **kw: False
    fake.rerun = lambda: None
    fake.session_state = {}
    fake.sidebar = _NoOp()
    fake.expander = lambda *a, **kw: _NoOp()
    fake.spinner = lambda *a, **kw: _NoOp()
    fake.container = lambda **kw: _NoOp()
    fake.columns = lambda spec, **kw: [_NoOp()] * (spec if isinstance(spec, int) else len(spec))
    fake.image = fake.download_button = lambda *a, **kw: None
    return fake, _Halt


def _load_harmonizer():
    real_st = sys.modules.get("streamlit")
    fake_st, halt_cls = _build_streamlit_stub()
    sys.modules["streamlit"] = fake_st
    try:
        spec = importlib.util.spec_from_file_location("harmonizer", _HARMONIZER_PATH)
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except halt_cls:
            pass
        return mod
    finally:
        # Restore whatever was in sys.modules["streamlit"] before — real module if we were
        # running inside Streamlit, or absent if we're headless.
        if real_st is not None:
            sys.modules["streamlit"] = real_st
        else:
            sys.modules.pop("streamlit", None)


_H = _load_harmonizer()
Settings = _H.Settings
harmonize = _H.harmonize


# ── Presets ──────────────────────────────────────────────────────────────

PACKSHOT_PRESET = Settings(
    strength=0.7,
    use_subject_mask=True,
    bg_normalize=True,
    bg_color_lo_hex="F4F4F5",
    bg_color_hi_hex="F9FAFF",
)

WORN_PRESET = Settings(
    strength=0.0,  # don't transfer color stats onto skin / lifestyle pixels
    use_subject_mask=True,
    bg_normalize=True,
    bg_color_lo_hex="F4F4F5",
    bg_color_hi_hex="F9FAFF",
)


def grade_image(
    image_bytes: bytes,
    classification: str,
    hero_rgb: Optional[np.ndarray] = None,
    custom_settings: Optional["Settings"] = None,  # type: ignore[name-defined]
) -> bytes:
    """
    Grade a single image with the appropriate preset.
    If hero_rgb is None, use the target itself as the hero (BG-only normalization still works).
    Returns PNG bytes.
    """
    from io import BytesIO
    img = Image.open(BytesIO(image_bytes)).convert("RGB")
    target_rgb = np.array(img)
    hero = hero_rgb if hero_rgb is not None else target_rgb

    if custom_settings is not None:
        settings = custom_settings
    elif classification == "worn":
        settings = WORN_PRESET
    else:
        settings = PACKSHOT_PRESET

    result_rgb, _ = harmonize(hero, target_rgb, settings)
    out_buf = BytesIO()
    Image.fromarray(result_rgb).save(out_buf, format="PNG")
    return out_buf.getvalue()
