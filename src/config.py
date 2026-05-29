"""Load environment variables and config.yaml into a typed Config dataclass."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv


@dataclass
class Config:
    # API keys
    anthropic_api_key: str = ""
    gemini_api_key: str = ""

    # Dropbox storage backend
    dropbox_token: str = ""          # short-lived access token (local/dev convenience)
    dropbox_app_key: str = ""        # OAuth app key   (durable refresh-token auth)
    dropbox_app_secret: str = ""     # OAuth app secret
    dropbox_refresh_token: str = ""  # OAuth refresh token (never expires)

    # Models
    anthropic_model: str = "claude-sonnet-4-5"
    gemini_model: str = "gemini-3-pro-image-preview"

    # Variants
    default_n: int = 4
    max_n: int = 10

    # Gemini output controls
    gemini_aspect_ratio: str = "1:1"
    gemini_image_size: str = "2K"
    gemini_rpm_cap: int = 200
    gemini_cost_per_image: float = 0.05
    gemini_batch_tier: bool = False
    gemini_max_retries: int = 3
    gemini_timeout_s: int = 90

    # Anthropic costs (per 1M tokens, USD)
    anthropic_classify_max_tokens: int = 16
    anthropic_prompt_max_tokens: int = 600
    anthropic_cost_input_per_mtok: float = 3.0
    anthropic_cost_output_per_mtok: float = 15.0
    anthropic_cost_cache_read_per_mtok: float = 0.3

    # Output
    output_suffix: str = "_OUT"
    output_format: str = "PNG"
    jpeg_quality: int = 95

    # Fixed prompts
    classifier_prompt: str = ""
    brief_packshot: str = ""
    brief_worn: str = ""

    # Root of the auto pipeline package
    root: Path = field(default_factory=lambda: Path(__file__).resolve().parent.parent)

    @property
    def effective_cost_per_image(self) -> float:
        return self.gemini_cost_per_image * (0.5 if self.gemini_batch_tier else 1.0)

    @property
    def product_refs_root(self) -> Path:
        return self.root / "prompts" / "product_refs"

    def product_refs_dir(self, product: str) -> Path:
        return self.product_refs_root / product

    def get_brief(self, classification: str) -> str:
        """
        Return the system-prompt brief for the classification ('packshot' or 'worn').
        Re-reads from disk on every call so a brief edit takes effect on next request
        even when an AnthropicClient is cached at module level.
        """
        name = "brief_worn.md" if classification == "worn" else "brief_packshot.md"
        path = self.root / "prompts" / name
        if path.exists():
            return path.read_text(encoding="utf-8")
        # Fall back to whatever was loaded at startup
        return self.brief_worn if classification == "worn" else self.brief_packshot

    REF_SLOTS: tuple[int, ...] = (2, 3, 4)
    REF_EXTS: tuple[str, ...] = (".png", ".jpg", ".jpeg", ".webp")

    def get_product_refs(self, product: str) -> dict[int, Path]:
        """
        Return {slot: path} for image2/3/4 that exist on disk for this product.
        Slot order matches the brief's [Image 2], [Image 3], [Image 4] convention.
        """
        d = self.product_refs_dir(product)
        out: dict[int, Path] = {}
        if not d.exists():
            return out
        for slot in self.REF_SLOTS:
            for ext in self.REF_EXTS:
                p = d / f"image{slot}{ext}"
                if p.exists():
                    out[slot] = p
                    break
        return out

    def get_product_refs_ordered(self, product: str) -> list[Path]:
        """List of ref paths in slot order (2, 3, 4) for direct use as content parts."""
        refs = self.get_product_refs(product)
        return [refs[s] for s in self.REF_SLOTS if s in refs]

    @property
    def dropbox_enabled(self) -> bool:
        return bool(self.dropbox_refresh_token and self.dropbox_app_key) or bool(self.dropbox_token)


def _secret(name: str, default: str = "") -> str:
    """Resolve a secret. Prefer st.secrets (Streamlit Cloud) and fall back to the
    process environment (.env locally). Safe to call outside a Streamlit runtime —
    st.secrets access raises there, so we swallow and fall through to os.environ."""
    try:
        import streamlit as st  # local import: keeps config usable in plain scripts
        val = st.secrets.get(name)  # type: ignore[attr-defined]
        if val:
            return str(val).strip()
    except Exception:
        pass
    return os.environ.get(name, default).strip()


def load_config() -> Config:
    """Load .env and config.yaml. Per-product briefs are read on demand via get_product_brief()."""
    root = Path(__file__).resolve().parent.parent
    # override=True so an empty value already in the process env (Claude Code / shells sometimes
    # pre-set ANTHROPIC_API_KEY="") doesn't shadow what's actually in our .env.
    load_dotenv(root / ".env", override=True)

    cfg_path = root / "config.yaml"
    cfg_yaml = {}
    if cfg_path.exists():
        cfg_yaml = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}

    variants = cfg_yaml.get("variants", {})
    models = cfg_yaml.get("models", {})
    gemini = cfg_yaml.get("gemini", {})
    anthropic = cfg_yaml.get("anthropic", {})
    output = cfg_yaml.get("output", {})

    cfg = Config(
        anthropic_api_key=_secret("ANTHROPIC_API_KEY"),
        gemini_api_key=_secret("GEMINI_API_KEY"),
        dropbox_token=_secret("DROPBOX_TOKEN"),
        dropbox_app_key=_secret("DROPBOX_APP_KEY"),
        dropbox_app_secret=_secret("DROPBOX_APP_SECRET"),
        dropbox_refresh_token=_secret("DROPBOX_REFRESH_TOKEN"),
        anthropic_model=models.get("anthropic", "claude-sonnet-4-5"),
        gemini_model=models.get("gemini", "gemini-3-pro-image-preview"),
        default_n=int(variants.get("default_n", 4)),
        max_n=int(variants.get("max_n", 10)),
        gemini_aspect_ratio=str(gemini.get("aspect_ratio", "1:1")),
        gemini_image_size=str(gemini.get("image_size", "2K")),
        gemini_rpm_cap=int(gemini.get("rpm_cap", 200)),
        gemini_cost_per_image=float(gemini.get("cost_per_image", 0.05)),
        gemini_batch_tier=bool(gemini.get("batch_tier", False)),
        gemini_max_retries=int(gemini.get("max_retries", 3)),
        gemini_timeout_s=int(gemini.get("timeout_s", 90)),
        anthropic_classify_max_tokens=int(anthropic.get("classify_max_tokens", 16)),
        anthropic_prompt_max_tokens=int(anthropic.get("prompt_max_tokens", 600)),
        anthropic_cost_input_per_mtok=float(anthropic.get("cost_input_per_mtok", 3.0)),
        anthropic_cost_output_per_mtok=float(anthropic.get("cost_output_per_mtok", 15.0)),
        anthropic_cost_cache_read_per_mtok=float(anthropic.get("cost_cache_read_per_mtok", 0.3)),
        output_suffix=str(output.get("suffix", "_OUT")),
        output_format=str(output.get("format", "PNG")).upper(),
        jpeg_quality=int(output.get("jpeg_quality", 95)),
    )

    prompts = root / "prompts"

    def _read(name: str) -> str:
        p = prompts / name
        return p.read_text(encoding="utf-8") if p.exists() else ""

    cfg.classifier_prompt = _read("classifier_prompt.md")
    cfg.brief_packshot = _read("brief_packshot.md")
    cfg.brief_worn = _read("brief_worn.md")

    return cfg
