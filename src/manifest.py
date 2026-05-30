"""Manifest = the persisted state for one output root. JSON, resumable.

Reads/writes go through a Storage backend (local disk or Dropbox), so the same
manifest logic works on the desktop and on the cloud deploy."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Literal, Optional

from .storage import Storage

Classification = Literal["packshot", "worn"]


@dataclass
class PhotoState:
    input_path: str                    # absolute path to source image
    product: str                       # subfolder name
    output_path: str                   # mirrored target path
    classification: Optional[str] = None              # "packshot" | "worn"
    user_overrode_classification: bool = False
    brief_notes: str = ""              # user's short description (e.g. "Skyline ring — three asscher diamonds")
    prompt: Optional[str] = None
    variants: list[str] = field(default_factory=list)         # all generated variant paths
    # Gallery model: variants are kept by default; the user soft-deletes weak ones
    # (paths added to hidden_variants — files stay until "Empty trash" purges them).
    # Grading is a separate, per-variant step: graded_variants maps a kept variant
    # path → its graded output path. `selected_variant`/`graded` are retained only
    # for migrating older single-select manifests (see Manifest.from_json).
    hidden_variants: list[str] = field(default_factory=list)
    graded_variants: dict = field(default_factory=dict)       # variant_path → graded output path
    selected_variant: Optional[str] = None
    graded: bool = False
    cost_usd: float = 0.0
    last_error: Optional[str] = None
    # Worn shots only: a generated packshot of the same product, used as the
    # SECOND Gemini reference so the worn render shows the correct product.
    # Storage path (e.g. a graded packshot output). None → fall back to the
    # static product reference files under prompts/product_refs/<product>/.
    product_ref_path: Optional[str] = None
    # Worn shots only: parametrized prompt support.
    #   worn_template — the analyzer's templated Nano Banana prompt with the six
    #       {PLACEHOLDER}s left literal. Cached per render (re-run only if cleared).
    #   worn_params   — the user's styling selections (param → option key), e.g.
    #       {"SKIN_TONE": "fair_warm_caucasian", ...}. Randomized at auto-prepare,
    #       editable per photo. The final `prompt` is assembled from these two.
    worn_template: Optional[str] = None
    worn_params: dict = field(default_factory=dict)

    @property
    def photo_id(self) -> str:
        # Stable id for keys: relative input path
        return f"{self.product}/{Path(self.input_path).name}"

    @property
    def kept_variants(self) -> list[str]:
        """Variants the user has NOT soft-deleted — what the gallery shows."""
        hidden = set(self.hidden_variants or [])
        return [v for v in (self.variants or []) if v not in hidden]

    def is_graded(self, variant_path: str) -> bool:
        return variant_path in (self.graded_variants or {})

    def display_for(self, variant_path: str) -> str:
        """The image to show for a kept variant: its graded output if it has one,
        otherwise the raw variant."""
        return (self.graded_variants or {}).get(variant_path, variant_path)


@dataclass
class Manifest:
    brand_root: str
    output_root: str
    photos: dict[str, PhotoState] = field(default_factory=dict)  # key = photo_id
    total_cost_usd: float = 0.0
    hero_path: Optional[str] = None        # color reference for grading; absolute path on disk
    hero_photo_id: Optional[str] = None    # which photo (if any) was promoted to hero; None for external uploads
    # ── Project-driven metadata (populated by pipeline.ingest from a Project) ──
    project_slug: Optional[str] = None     # slug of the Project that owns this manifest
    product_briefs: dict[str, str] = field(default_factory=dict)        # folder_name → description
    product_classifications: dict[str, str] = field(default_factory=dict)  # folder_name → "packshot"|"worn"
    # Per-product hero override. Photos in this product folder grade against the
    # path stored here INSTEAD of `hero_path`. Useful when one product needs a
    # different color reference (e.g. mixed metals across the catalog).
    product_heroes: dict[str, str] = field(default_factory=dict)
    # Last grade parameters applied for a product (strength/whiten/warmth/gold/cool),
    # reused as the default when grading other variants of the same product so the
    # art director tunes once and re-applies quickly across the set.
    product_grade_params: dict = field(default_factory=dict)
    # Cached folder discovery: [[product, image_path], ...]. Populated by ingest so a
    # re-open doesn't re-list every product folder on the backend (slow on Dropbox).
    # Refreshed only when the user invokes "Rescan folder".
    discovered: list = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(
            {
                "brand_root": self.brand_root,
                "output_root": self.output_root,
                "photos": {k: asdict(v) for k, v in self.photos.items()},
                "total_cost_usd": self.total_cost_usd,
                "hero_path": self.hero_path,
                "hero_photo_id": self.hero_photo_id,
                "project_slug": self.project_slug,
                "product_briefs": self.product_briefs,
                "product_classifications": self.product_classifications,
                "product_heroes": self.product_heroes,
                "product_grade_params": self.product_grade_params,
                "discovered": self.discovered,
            },
            indent=2,
        )

    @classmethod
    def from_json(cls, data: str) -> "Manifest":
        obj = json.loads(data)
        photos = {k: PhotoState(**v) for k, v in obj.get("photos", {}).items()}
        # Migrate older single-select manifests to the keep-all gallery model: a
        # photo that was graded against one picked variant becomes one entry in
        # graded_variants. Idempotent — only fills when graded_variants is empty.
        for p in photos.values():
            if not p.graded_variants and p.graded and p.selected_variant:
                p.graded_variants = {p.selected_variant: p.output_path}
        return cls(
            brand_root=obj["brand_root"],
            output_root=obj["output_root"],
            photos=photos,
            total_cost_usd=float(obj.get("total_cost_usd", 0.0)),
            hero_path=obj.get("hero_path"),
            hero_photo_id=obj.get("hero_photo_id"),
            project_slug=obj.get("project_slug"),
            product_briefs=dict(obj.get("product_briefs", {})),
            product_classifications=dict(obj.get("product_classifications", {})),
            product_heroes=dict(obj.get("product_heroes", {})),
            product_grade_params=dict(obj.get("product_grade_params", {})),
            discovered=list(obj.get("discovered", [])),
        )


def manifest_path(storage: Storage, output_root: str) -> str:
    return storage.join(output_root, ".pipeline_manifest.json")


def save(manifest: Manifest, storage: Storage) -> None:
    """Persist the manifest JSON beside the output root (atomicity handled by the backend)."""
    target = manifest_path(storage, manifest.output_root)
    storage.write_text(target, manifest.to_json())


def load(output_root: str, storage: Storage) -> Optional[Manifest]:
    p = manifest_path(storage, output_root)
    if not storage.exists(p):
        return None
    return Manifest.from_json(storage.read_text(p))
