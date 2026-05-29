"""
Project = a saved (brand_folder, output_folder, per-product briefs) bundle.

Lives under `_auto_pipeline/projects/<slug>.json`. The landing screen lists these,
and the user picks one to open or creates a new one.

A `ProductGroup` corresponds to one subfolder of the brand folder (e.g. `01_Skyline/`).
Its `description` is a short human-written sentence that Claude reads as the first
line of the system prompt so it doesn't mis-identify the jewelry from vision alone:

    The image attached is: "a ring in gold and diamonds"

`is_worn` is inferred from the folder name (contains "worn") at project creation
and locked in — so classification is folder-name driven, no Claude vision call needed.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"}

# Folder names containing any of these tokens (case-insensitive) are treated as
# worn-jewelry folders. Anything else is a packshot folder.
_WORN_TOKENS = ("worn", "model", "lifestyle", "_on_")


def is_worn_folder(folder_name: str) -> bool:
    """Classify a folder by its name. Case-insensitive."""
    lowered = folder_name.lower()
    return any(tok in lowered for tok in _WORN_TOKENS)


def _slugify(name: str) -> str:
    s = re.sub(r"[^\w\-]+", "_", name.strip(), flags=re.UNICODE)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "project"


@dataclass
class ProductGroup:
    folder_name: str                   # subfolder under brand_root (e.g. "01_Skyline")
    description: str = ""              # user-provided product description (e.g. "a ring in gold and diamonds")
    is_worn: bool = False              # classification: True = worn, False = packshot
    n_images: int = 0                  # number of images discovered at project creation (informational)


@dataclass
class Project:
    name: str
    brand_root: str                    # absolute path
    output_root: str                   # absolute path
    products: list[ProductGroup] = field(default_factory=list)
    created_at: str = ""               # ISO timestamp
    updated_at: str = ""

    # --- Convenience ---

    @property
    def slug(self) -> str:
        return _slugify(self.name)

    def product_brief_map(self) -> dict[str, str]:
        """{folder_name: description} — what the pipeline reads to inject into Claude's prompt."""
        return {p.folder_name: p.description for p in self.products}

    def product_classification_map(self) -> dict[str, str]:
        """{folder_name: 'packshot' | 'worn'} — drives which brief Claude uses."""
        return {p.folder_name: ("worn" if p.is_worn else "packshot") for p in self.products}

    def find_product(self, folder_name: str) -> Optional[ProductGroup]:
        for p in self.products:
            if p.folder_name == folder_name:
                return p
        return None

    # --- (de)serialization ---

    def to_json(self) -> str:
        return json.dumps(
            {
                "name": self.name,
                "brand_root": self.brand_root,
                "output_root": self.output_root,
                "products": [asdict(p) for p in self.products],
                "created_at": self.created_at,
                "updated_at": self.updated_at,
            },
            indent=2,
        )

    @classmethod
    def from_json(cls, data: str) -> "Project":
        obj = json.loads(data)
        return cls(
            name=obj["name"],
            brand_root=obj["brand_root"],
            output_root=obj["output_root"],
            products=[ProductGroup(**p) for p in obj.get("products", [])],
            created_at=obj.get("created_at", ""),
            updated_at=obj.get("updated_at", ""),
        )


# ─── Discovery ────────────────────────────────────────────────────────────

def discover_product_groups(brand_root: Path) -> list[ProductGroup]:
    """
    Walk the brand folder and produce one ProductGroup per subfolder that contains
    images. The description is left blank — the user fills these in during the
    "Create new project" wizard.

    Folder order: alphabetical (sorted). Numbered prefixes like "01_", "02_" sort
    naturally, which matches how the user organizes them.
    """
    out: list[ProductGroup] = []
    if not brand_root.exists() or not brand_root.is_dir():
        return out
    for sub in sorted(p for p in brand_root.iterdir() if p.is_dir()):
        # Count images directly inside (one level deep — match pipeline.discover_images)
        n = sum(1 for f in sub.iterdir() if f.is_file() and f.suffix.lower() in IMAGE_EXTS)
        if n == 0:
            continue
        out.append(
            ProductGroup(
                folder_name=sub.name,
                description="",
                is_worn=is_worn_folder(sub.name),
                n_images=n,
            )
        )
    return out


# ─── Persistence ──────────────────────────────────────────────────────────

def _projects_dir() -> Path:
    # Lives in _auto_pipeline/projects/ next to the package.
    d = Path(__file__).resolve().parent.parent / "projects"
    d.mkdir(parents=True, exist_ok=True)
    return d


def project_path(slug: str) -> Path:
    return _projects_dir() / f"{slug}.json"


def _atomic_write(path: Path, payload: str) -> None:
    """tmp + rename with retries — same hardening as manifest.py for Dropbox/AV locks."""
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    last_exc: Optional[BaseException] = None
    for attempt in range(5):
        tmp_fd, tmp_path = tempfile.mkstemp(prefix=".project_", suffix=".json", dir=str(parent))
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                f.write(payload)
            os.replace(tmp_path, path)
            return
        except OSError as e:
            last_exc = e
            try:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except OSError:
                pass
            time.sleep(0.1 * (2 ** attempt))
        except Exception:
            try:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except OSError:
                pass
            raise
    # Last resort: direct write
    with open(path, "w", encoding="utf-8") as f:
        f.write(payload)
    if last_exc is not None:
        # We recovered, but flag it once on stderr so the user sees it in the terminal
        import sys
        print(f"[project] atomic write retried, fell back to direct write: {last_exc}", file=sys.stderr)


def save_project(project: Project) -> Path:
    project.updated_at = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    if not project.created_at:
        project.created_at = project.updated_at
    target = project_path(project.slug)
    _atomic_write(target, project.to_json())
    return target


def load_project(slug: str) -> Optional[Project]:
    p = project_path(slug)
    if not p.exists():
        return None
    return Project.from_json(p.read_text(encoding="utf-8"))


def delete_project(slug: str) -> bool:
    p = project_path(slug)
    if p.exists():
        try:
            p.unlink()
            return True
        except OSError:
            return False
    return False


@dataclass
class ProjectSummary:
    slug: str
    name: str
    brand_root: str
    output_root: str
    n_products: int
    updated_at: str


def list_projects() -> list[ProjectSummary]:
    """All saved projects, newest-updated first."""
    d = _projects_dir()
    out: list[ProjectSummary] = []
    for f in d.glob("*.json"):
        try:
            obj = json.loads(f.read_text(encoding="utf-8"))
            out.append(
                ProjectSummary(
                    slug=f.stem,
                    name=obj.get("name", f.stem),
                    brand_root=obj.get("brand_root", ""),
                    output_root=obj.get("output_root", ""),
                    n_products=len(obj.get("products", [])),
                    updated_at=obj.get("updated_at", ""),
                )
            )
        except Exception:
            # Skip malformed entries silently
            continue
    out.sort(key=lambda s: s.updated_at, reverse=True)
    return out
