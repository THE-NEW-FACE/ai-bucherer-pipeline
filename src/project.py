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
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import Config
from .storage import Storage, get_storage


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

def discover_product_groups(cfg: Config, brand_root: str) -> list[ProductGroup]:
    """
    Walk the brand folder and produce one ProductGroup per subfolder that contains
    images. The description is left blank — the user fills these in during the
    "Create new project" wizard.

    Folder order: sorted by the storage backend. Numbered prefixes like "01_", "02_"
    sort naturally, which matches how the user organizes them.
    """
    st = get_storage(cfg)
    out: list[ProductGroup] = []
    if not st.exists(brand_root) or not st.is_dir(brand_root):
        return out
    for sub in st.list_subdirs(brand_root):
        # Count images directly inside (one level deep — match pipeline.discover_images)
        n = sum(
            1 for f in st.list_files(sub)
            if Path(st.name(f)).suffix.lower() in IMAGE_EXTS
        )
        if n == 0:
            continue
        name = st.name(sub)
        out.append(
            ProductGroup(
                folder_name=name,
                description="",
                is_worn=is_worn_folder(name),
                n_images=n,
            )
        )
    return out


# ─── Persistence ──────────────────────────────────────────────────────────

def _projects_root(cfg: Config) -> str:
    """Where project JSONs live: the configured Dropbox dir on the cloud backend,
    else the local _auto_pipeline/projects/ folder next to the package."""
    st = get_storage(cfg)
    if getattr(st, "backend", "local") == "dropbox" and cfg.dropbox_projects_dir:
        return cfg.dropbox_projects_dir
    d = Path(__file__).resolve().parent.parent / "projects"
    return str(d)


def project_path(cfg: Config, slug: str) -> str:
    st = get_storage(cfg)
    return st.join(_projects_root(cfg), f"{slug}.json")


def save_project(cfg: Config, project: Project) -> str:
    project.updated_at = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    if not project.created_at:
        project.created_at = project.updated_at
    st = get_storage(cfg)
    target = project_path(cfg, project.slug)
    st.write_text(target, project.to_json())
    return target


def load_project(cfg: Config, slug: str) -> Optional[Project]:
    st = get_storage(cfg)
    p = project_path(cfg, slug)
    if not st.exists(p):
        return None
    return Project.from_json(st.read_text(p))


def delete_project(cfg: Config, slug: str) -> bool:
    st = get_storage(cfg)
    return st.delete(project_path(cfg, slug))


@dataclass
class ProjectSummary:
    slug: str
    name: str
    brand_root: str
    output_root: str
    n_products: int
    updated_at: str


def list_projects(cfg: Config) -> list[ProjectSummary]:
    """All saved projects, newest-updated first."""
    st = get_storage(cfg)
    root = _projects_root(cfg)
    out: list[ProjectSummary] = []
    if not st.exists(root):
        return out
    for f in st.list_files(root):
        if not st.name(f).endswith(".json"):
            continue
        try:
            obj = json.loads(st.read_text(f))
            out.append(
                ProjectSummary(
                    slug=st.stem(f),
                    name=obj.get("name", st.stem(f)),
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
