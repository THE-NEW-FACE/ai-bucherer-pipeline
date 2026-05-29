"""Manifest = the on-disk state for one output root. JSON, atomic writes, resumable."""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Literal, Optional

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
    variants: list[str] = field(default_factory=list)         # paths in workspace
    selected_variant: Optional[str] = None
    graded: bool = False
    cost_usd: float = 0.0
    last_error: Optional[str] = None

    @property
    def photo_id(self) -> str:
        # Stable id for keys: relative input path
        return f"{self.product}/{Path(self.input_path).name}"


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
            },
            indent=2,
        )

    @classmethod
    def from_json(cls, data: str) -> "Manifest":
        obj = json.loads(data)
        photos = {k: PhotoState(**v) for k, v in obj.get("photos", {}).items()}
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
        )


def manifest_path(output_root: Path) -> Path:
    return output_root / ".pipeline_manifest.json"


def _try_unlink(p: str) -> None:
    try:
        if os.path.exists(p):
            os.unlink(p)
    except OSError:
        # Best-effort cleanup; another process (Dropbox, antivirus) may still hold it.
        pass


def save(manifest: Manifest) -> None:
    """Atomic write: tmp + rename, with retries for Dropbox/AV/OneDrive locks on Windows."""
    out = Path(manifest.output_root)
    out.mkdir(parents=True, exist_ok=True)
    target = manifest_path(out)
    payload = manifest.to_json()

    # Try atomic tmp+rename a few times — Dropbox sync, antivirus, or OneDrive
    # often grab a brief read lock the moment the temp file appears, causing
    # os.replace to fail with WinError 5 (Access denied) or WinError 32 (file in use).
    last_exc: Optional[BaseException] = None
    for attempt in range(5):
        tmp_fd, tmp_path = tempfile.mkstemp(prefix=".manifest_", suffix=".json", dir=str(out))
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                f.write(payload)
            os.replace(tmp_path, target)
            return
        except OSError as e:
            last_exc = e
            _try_unlink(tmp_path)
            # Exponential backoff: 0.1s, 0.2s, 0.4s, 0.8s, 1.6s
            time.sleep(0.1 * (2 ** attempt))
        except Exception:
            _try_unlink(tmp_path)
            raise

    # Last resort: write directly to the target. Not atomic, but a corrupted
    # manifest is still better than losing the entire session's state.
    try:
        with open(target, "w", encoding="utf-8") as f:
            f.write(payload)
    except OSError as e:
        raise OSError(
            f"Could not save manifest to {target}. Last atomic-write error: {last_exc}. "
            f"Direct write also failed: {e}. "
            f"If the output folder is in Dropbox/OneDrive, try pausing sync or moving the output root outside the sync folder."
        ) from e


def load(output_root: Path) -> Optional[Manifest]:
    p = manifest_path(output_root)
    if not p.exists():
        return None
    return Manifest.from_json(p.read_text(encoding="utf-8"))
