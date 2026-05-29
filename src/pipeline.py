"""Orchestrator: ingest folders, classify, generate, grade. Manifest-driven, resumable."""

from __future__ import annotations

import hashlib
import re
import shutil
import tempfile
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Iterable, Optional

from . import manifest as M
from .anthropic_client import AnthropicClient
from .config import Config
from .gemini_client import GeminiClient
from .grader import grade_image
from .project import Project
from .storage import Storage, get_storage

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"}

# ── Fenced-block extraction ─────────────────────────────────────────────
# Kept as a utility — current pipeline passes Claude's full output to Gemini verbatim,
# but this helper is here if you ever want to strip the fence wrapper.
_FENCE_RE = re.compile(r"```(?:[\w-]*\n)?(.*?)```", re.DOTALL)


def extract_prompt_block(claude_output: str) -> str:
    """If Claude wrapped the Nano Banana prompt in a fenced code block, extract it.
    Otherwise return the input unchanged."""
    if not claude_output:
        return claude_output
    m = _FENCE_RE.search(claude_output)
    if m:
        return m.group(1).strip()
    return claude_output.strip()


# ── Aspect ratio detection ──────────────────────────────────────────────
# Gemini 3 Pro Image accepts these exact strings — pick the closest match to the
# input render's actual proportions so the output isn't squashed/letterboxed.
_SUPPORTED_ASPECTS = {
    "1:1":   1.0,
    "5:4":   1.25,
    "4:3":   4 / 3,
    "3:2":   1.5,
    "16:9":  16 / 9,
    "21:9":  21 / 9,
    "4:5":   0.8,
    "3:4":   0.75,
    "2:3":   2 / 3,
    "9:16":  9 / 16,
}


def detect_aspect_ratio(image_path: Path) -> str:
    """Read the input image, return the Gemini-supported aspect ratio string closest to
    its actual width/height. Defaults to '1:1' if the file can't be opened."""
    try:
        from PIL import Image as _PIL
        with _PIL.open(image_path) as img:
            w, h = img.size
        if h <= 0:
            return "1:1"
        ratio = w / h
        return min(_SUPPORTED_ASPECTS.items(), key=lambda kv: abs(kv[1] - ratio))[0]
    except Exception:
        return "1:1"


# ── Background executors for parallel API calls ─────────────────────────
# Two separate pools so Claude prompt jobs (~3-10s) don't get blocked waiting for
# Gemini variant jobs (~10-30s). Each photo's worker still parallelizes its inner
# work (Gemini fans out N variants in its own ThreadPoolExecutor).
_REGEN_EXECUTOR: Optional[ThreadPoolExecutor] = None
_PROMPT_EXECUTOR: Optional[ThreadPoolExecutor] = None
_GRADE_EXECUTOR: Optional[ThreadPoolExecutor] = None
_MANIFEST_LOCK = threading.Lock()


def _get_executor() -> ThreadPoolExecutor:
    """Lazily create the variant-regenerate executor — 4 workers means up to 4 photos
    regenerate in parallel (each spawns its own internal pool for variants)."""
    global _REGEN_EXECUTOR
    if _REGEN_EXECUTOR is None:
        _REGEN_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="regen")
    return _REGEN_EXECUTOR


def _get_prompt_executor() -> ThreadPoolExecutor:
    """Lazily create the Claude prompt-generation executor.
    4 workers covers a comfortable burst of clicks without overshooting the API."""
    global _PROMPT_EXECUTOR
    if _PROMPT_EXECUTOR is None:
        _PROMPT_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="prompt")
    return _PROMPT_EXECUTOR


def _get_grade_executor() -> ThreadPoolExecutor:
    """Lazily create the local grading executor — 2 workers, since grading is CPU-bound
    (PIL ops + LAB color transfer) and runs entirely on the user's machine."""
    global _GRADE_EXECUTOR
    if _GRADE_EXECUTOR is None:
        _GRADE_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="grade")
    return _GRADE_EXECUTOR


# ── Ingest ──────────────────────────────────────────────────────────────

def discover_images(cfg: Config, brand_root: str) -> list[tuple[str, str]]:
    """Return [(product_name, image_path)] for every image in brand_root/<product>/."""
    st = get_storage(cfg)
    out: list[tuple[str, str]] = []
    if not st.exists(brand_root):
        return out
    for product_dir in st.list_subdirs(brand_root):
        product = st.name(product_dir)
        for img in st.list_files(product_dir):
            if Path(st.name(img)).suffix.lower() in IMAGE_EXTS:
                out.append((product, img))
    return out


def default_output_root(cfg: Config, brand_root: str) -> str:
    st = get_storage(cfg)
    parent = st.parent(brand_root)
    return st.join(parent, f"{st.name(brand_root)}{cfg.output_suffix}")


def ingest(
    cfg: Config,
    brand_root: str,
    output_root: str,
    project: Optional[Project] = None,
) -> M.Manifest:
    """Build (or top-up) manifest from the brand folder.

    If `project` is supplied, its per-product briefs and classifications are written
    into the manifest, and new photos get their `classification` and `brief_notes`
    pre-filled from the matching ProductGroup — no Claude vision call needed.
    """
    st = get_storage(cfg)
    brand_root = str(brand_root)
    output_root = str(output_root)

    existing = M.load(output_root, st)
    if existing and existing.brand_root == brand_root:
        manifest = existing
    else:
        manifest = M.Manifest(brand_root=brand_root, output_root=output_root)

    # If a project is provided, sync its briefs + classifications into the manifest.
    # This keeps the manifest a self-contained source of truth (so the pipeline can
    # run even if the Project JSON is deleted).
    if project is not None:
        manifest.project_slug = project.slug
        manifest.product_briefs = project.product_brief_map()
        manifest.product_classifications = project.product_classification_map()

    for product, img in discover_images(cfg, brand_root):
        rel_under_brand = st.relpath(img, brand_root)
        out_path = st.join(output_root, rel_under_brand)
        photo_id = f"{product}/{st.name(img)}"
        if photo_id not in manifest.photos:
            # Pre-fill classification from the project's product map (skips Claude vision
            # classify call). brief_notes stays empty — the per-product description lives
            # in manifest.product_briefs and is injected as the SYSTEM-prompt prefix, not
            # as a per-photo user note. brief_notes is reserved for per-photo overrides.
            preset_class = manifest.product_classifications.get(product)
            manifest.photos[photo_id] = M.PhotoState(
                input_path=img,
                product=product,
                output_path=out_path,
                classification=preset_class,        # None if no project supplied
            )

    # Reconcile transient variants. Older manifests (and any pre-persistence run)
    # recorded variant paths in an ephemeral local scratch dir that no longer exists
    # — e.g. a previous cloud container's /tmp. Drop dangling references so the board
    # and the detail view agree and the user is cleanly prompted to regenerate. We
    # probe only the first variant per photo (they share a dir, so they live or die
    # together) to keep this to one existence check per photo. The graded output is
    # persisted separately, so `graded`/`output_path` are left untouched.
    for photo in manifest.photos.values():
        if photo.variants and not st.exists(photo.variants[0]):
            photo.variants = []
            photo.selected_variant = None

    st.makedirs(output_root)
    M.save(manifest, st)
    return manifest


# ── Workspace paths ─────────────────────────────────────────────────────
# Variants are transient scratch — kept on a real local disk (even when the
# storage backend is Dropbox) so we don't litter the cloud folder with throwaway
# renders or pay upload/download latency on every regenerate. Only the chosen,
# graded output is persisted through Storage.

def _workspace_base(output_root: str) -> Path:
    key = hashlib.sha256(str(output_root).encode("utf-8")).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / "ai_bucherer_workspace" / key


def workspace_dir(output_root: str, product: str, photo_stem: str) -> Path:
    return _workspace_base(output_root) / product / photo_stem


def clear_workspace_for(output_root: str, photo: M.PhotoState) -> None:
    stem = Path(photo.input_path).stem
    ws = workspace_dir(output_root, photo.product, stem)
    if ws.exists():
        shutil.rmtree(ws, ignore_errors=True)


def tidy_all_workspaces(cfg: Config, manifest: M.Manifest) -> int:
    """Delete workspace dirs for photos whose selection is already on disk. Returns count cleaned."""
    st = get_storage(cfg)
    cleaned = 0
    for photo in manifest.photos.values():
        if photo.graded and photo.selected_variant:
            stem = Path(photo.input_path).stem
            ws = workspace_dir(manifest.output_root, photo.product, stem)
            if ws.exists():
                shutil.rmtree(ws, ignore_errors=True)
                cleaned += 1
    photo.variants = []  # type: ignore[possibly-undefined]
    M.save(manifest, st)
    return cleaned


# ── Stage runners ───────────────────────────────────────────────────────

def classify_photo(
    cfg: Config,
    manifest: M.Manifest,
    photo: M.PhotoState,
    anthropic_client: AnthropicClient,
) -> None:
    """Assign packshot|worn classification.

    Fast path: if the manifest has a project-supplied classification for this
    photo's product folder, use it (no API call). Otherwise fall back to a
    Claude vision call.
    """
    if photo.classification is not None:
        return
    st = get_storage(cfg)
    preset = manifest.product_classifications.get(photo.product)
    if preset:
        photo.classification = preset
        M.save(manifest, st)
        return
    res = anthropic_client.classify(Path(st.materialize(photo.input_path)))
    photo.classification = res.text
    photo.cost_usd += res.cost_usd
    manifest.total_cost_usd += res.cost_usd
    M.save(manifest, st)


def generate_prompt_for(
    cfg: Config,
    manifest: M.Manifest,
    photo: M.PhotoState,
    anthropic_client: AnthropicClient,
) -> None:
    if photo.prompt:
        return
    st = get_storage(cfg)
    if photo.classification is None:
        classify_photo(cfg, manifest, photo, anthropic_client)
    classification = photo.classification or "packshot"
    # Packshot shots use ONLY the input 3D render as reference (Image 1).
    # Worn shots use Image 2/3/4 from prompts/product_refs/<product>/.
    refs = cfg.get_product_refs(photo.product) if classification == "worn" else {}
    # Per-product description (set at project creation), prepended to the brief.
    product_description = manifest.product_briefs.get(photo.product, "")
    res = anthropic_client.generate_prompt(
        Path(st.materialize(photo.input_path)),
        classification=classification,
        refs=refs,
        brief_notes=photo.brief_notes or "",
        product_description=product_description,
    )
    with _MANIFEST_LOCK:
        photo.prompt = res.text
        photo.cost_usd += res.cost_usd
        manifest.total_cost_usd += res.cost_usd
        # Flag truncation so the UI can surface a clear warning.
        # stop_reason: "end_turn" = clean finish; "max_tokens" = output was cut off.
        if res.stop_reason == "max_tokens":
            photo.last_error = (
                f"⚠ Claude output truncated at {res.output_tokens} tokens. "
                f"Bump anthropic.prompt_max_tokens in config.yaml and click 🧠 (Re)generate prompt."
            )
        else:
            # Clear any previous truncation warning on successful clean output
            if photo.last_error and "truncated" in photo.last_error:
                photo.last_error = None
        M.save(manifest, st)


def generate_variants_for(
    cfg: Config,
    manifest: M.Manifest,
    photo: M.PhotoState,
    gemini_client: GeminiClient,
    n: Optional[int] = None,
    reference_override: Optional[Path] = None,
) -> None:
    """Run Gemini batch. Writes N images to workspace, sets photo.variants, clears selection."""
    if not photo.prompt:
        raise RuntimeError(f"Cannot generate variants for {photo.photo_id}: prompt is empty")
    st = get_storage(cfg)
    n = n or cfg.default_n
    n = max(1, min(n, cfg.max_n))

    stem = Path(photo.input_path).stem
    # Variants are persisted through Storage (output_root/.variants/<product>/<stem>)
    # so they survive process/container restarts — essential on the ephemeral cloud
    # host, where a local /tmp scratch dir is wiped between sessions (which silently
    # lost every variant). Clear any prior renders for this photo before writing new.
    vdir = st.join(manifest.output_root, ".variants", photo.product, stem)
    for old in st.list_files(vdir):
        st.delete(old)

    # Gemini needs a real local file. reference_override may be a local workspace path
    # (already real) or a storage path; materialize handles both (no-op for local).
    raw_ref = str(reference_override) if reference_override else photo.input_path
    input_ref = Path(st.materialize(raw_ref))
    # Refs only feed into Gemini for worn shots. The SECOND reference (Image 2) is a
    # generated packshot of the product when the user has picked one on the detail
    # page (photo.product_ref_path); otherwise fall back to the static product-ref
    # files under prompts/product_refs/<product>/.
    additional: list[Path] = []
    if photo.classification == "worn":
        if photo.product_ref_path and st.exists(photo.product_ref_path):
            additional = [Path(st.materialize(photo.product_ref_path))]
        else:
            additional = cfg.get_product_refs_ordered(photo.product)
    # Aspect ratio is locked to config.yaml (1:1) — both briefs enforce 1:1 in their
    # output instruction, so no per-call override is needed.
    batch = gemini_client.generate_n(
        photo.prompt,
        input_reference=input_ref,
        n=n,
        additional_refs=additional,
    )

    paths: list[str] = []
    for i, r in enumerate(batch.results):
        if r is None:
            continue
        vp = st.join(vdir, f"variant_{i + 1:03d}.png")
        st.write_bytes(vp, r.image_bytes)
        paths.append(vp)

    # Lock the manifest mutation + save so parallel regenerates on different photos
    # don't lose total_cost_usd updates or corrupt the JSON write.
    with _MANIFEST_LOCK:
        photo.variants = paths
        photo.selected_variant = None
        photo.graded = False
        photo.cost_usd += batch.cost_usd
        photo.last_error = "; ".join(batch.errors) if batch.errors else None
        manifest.total_cost_usd += batch.cost_usd
        M.save(manifest, st)


def submit_regenerate(
    cfg: Config,
    manifest: M.Manifest,
    photo: M.PhotoState,
    anthropic_client: AnthropicClient,
    gemini_client: GeminiClient,
    prompt_override: Optional[str] = None,
    reference_override: Optional[Path] = None,
    n: Optional[int] = None,
) -> Future:
    """
    Kick off a regenerate in the shared background executor. Returns a Future so callers
    can poll completion without blocking the UI. Errors are captured into photo.last_error
    AND re-raised through the Future, so the UI can show them.
    """
    def _task():
        try:
            regenerate(
                cfg, manifest, photo, anthropic_client, gemini_client,
                prompt_override=prompt_override,
                reference_override=reference_override,
                n=n,
            )
        except Exception as e:
            with _MANIFEST_LOCK:
                photo.last_error = str(e)
                M.save(manifest, get_storage(cfg))
            raise

    return _get_executor().submit(_task)


def submit_generate_prompt(
    cfg: Config,
    manifest: M.Manifest,
    photo: M.PhotoState,
    anthropic_client: AnthropicClient,
    force: bool = True,
) -> Future:
    """
    Kick off Claude classify + prompt generation in the prompt executor.
    Returns a Future so the UI can poll completion without blocking.
    `force=True` (default) wipes photo.prompt first so generate_prompt_for actually re-runs.
    """
    def _task():
        try:
            if photo.classification is None:
                classify_photo(cfg, manifest, photo, anthropic_client)
            if force:
                with _MANIFEST_LOCK:
                    photo.prompt = None
            generate_prompt_for(cfg, manifest, photo, anthropic_client)
        except Exception as e:
            with _MANIFEST_LOCK:
                photo.last_error = str(e)
                M.save(manifest, get_storage(cfg))
            raise

    return _get_prompt_executor().submit(_task)


def hero_path_for_photo(cfg: Config, manifest: M.Manifest, photo: M.PhotoState) -> Optional[str]:
    """Resolve the grading reference for a photo.

    Priority:
      1. Per-product override (`manifest.product_heroes[photo.product]`)
      2. Project-wide default (`manifest.hero_path`)
      3. None (grading runs background-only)
    """
    st = get_storage(cfg)
    per_product = manifest.product_heroes.get(photo.product)
    if per_product and st.exists(per_product):
        return per_product
    if manifest.hero_path and st.exists(manifest.hero_path):
        return manifest.hero_path
    return None


def select_variant(
    cfg: Config,
    manifest: M.Manifest,
    photo: M.PhotoState,
    variant_path: str,
) -> None:
    """Grade selected variant against the relevant hero, write to output_path, mark graded.

    Hero selection: per-product override → project-wide default → none.
    Without a hero, grading falls back to background-normalization only (no color transfer).
    """
    if variant_path not in photo.variants:
        raise ValueError(f"variant_path {variant_path} not in current variants for {photo.photo_id}")

    st = get_storage(cfg)
    # Variants are persisted through Storage (see generate_variants_for) — read via
    # the backend so this works for both local disk and Dropbox.
    src_bytes = st.read_bytes(variant_path)

    hero_rgb = None
    hero_resolved = hero_path_for_photo(cfg, manifest, photo)
    if hero_resolved:
        import numpy as np
        from PIL import Image as _PI
        hero_img = _PI.open(st.materialize(hero_resolved)).convert("RGB")
        hero_rgb = np.array(hero_img)

    graded = grade_image(src_bytes, photo.classification or "packshot", hero_rgb=hero_rgb)
    st.write_bytes(photo.output_path, graded)

    photo.selected_variant = variant_path
    photo.graded = True
    M.save(manifest, st)


def submit_grade(
    cfg: Config,
    manifest: M.Manifest,
    photo: M.PhotoState,
    variant_path: str,
) -> Future:
    """Kick off grading in the background. Returns a Future the UI polls.
    Heavy (PIL + LAB transfer + disk I/O) — running it on the UI thread freezes Streamlit
    for ~5–15s per pick, which is the worst part of the experience to keep blocking."""
    def _task():
        try:
            select_variant(cfg, manifest, photo, variant_path)
        except Exception as e:
            with _MANIFEST_LOCK:
                photo.last_error = f"Grading failed: {e}"
                M.save(manifest, get_storage(cfg))
            raise

    return _get_grade_executor().submit(_task)


def regrade_all_selected(cfg: Config, manifest: M.Manifest) -> int:
    """Re-grade every photo that has a selected variant, using the current manifest.hero_path.
    Returns count of re-graded photos. Local-only — no API calls, no cost."""
    n = 0
    for photo in manifest.photos.values():
        if photo.selected_variant and Path(photo.selected_variant).exists():
            select_variant(cfg, manifest, photo, photo.selected_variant)
            n += 1
    return n


def regenerate(
    cfg: Config,
    manifest: M.Manifest,
    photo: M.PhotoState,
    anthropic_client: AnthropicClient,
    gemini_client: GeminiClient,
    prompt_override: Optional[str] = None,
    reference_override: Optional[Path] = None,
    n: Optional[int] = None,
) -> None:
    if prompt_override is not None:
        photo.prompt = prompt_override
    if not photo.prompt:
        generate_prompt_for(cfg, manifest, photo, anthropic_client)
    generate_variants_for(
        cfg, manifest, photo, gemini_client, n=n, reference_override=reference_override
    )


# ── Bulk operations ─────────────────────────────────────────────────────

def process_pending(
    cfg: Config,
    manifest: M.Manifest,
    anthropic_client: AnthropicClient,
    gemini_client: GeminiClient,
    n: Optional[int] = None,
    progress_cb: Optional[Callable[[str, int, int], None]] = None,
) -> None:
    """For each photo without variants yet, run classify → prompt → generate_n."""
    pending = [p for p in manifest.photos.values() if not p.variants]
    total = len(pending)
    for idx, photo in enumerate(pending, 1):
        if progress_cb:
            progress_cb(photo.photo_id, idx, total)
        try:
            if photo.classification is None:
                classify_photo(cfg, manifest, photo, anthropic_client)
            if not photo.prompt:
                generate_prompt_for(cfg, manifest, photo, anthropic_client)
            generate_variants_for(cfg, manifest, photo, gemini_client, n=n)
        except Exception as e:
            photo.last_error = str(e)
            M.save(manifest, get_storage(cfg))
