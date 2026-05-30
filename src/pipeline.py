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
from . import worn_params as WP
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
# Guards in-memory manifest MUTATIONS only — NOT the network write. Holding a global
# lock across a Dropbox upload serialized every worker (the manifest carries large
# worn templates now), which made parallel regenerates look sequential.
_MANIFEST_LOCK = threading.Lock()


def _persist(cfg: Config, manifest: M.Manifest) -> None:
    """Save the manifest without serializing workers on the network write.

    The JSON snapshot is taken under `_MANIFEST_LOCK` (microseconds, keeps the
    snapshot internally consistent), but the actual Storage upload happens OUTSIDE
    the lock so concurrent workers don't queue behind each other's Dropbox writes.
    Concurrent uploads are last-writer-wins on a shared object — every snapshot is a
    full consistent state, and the in-memory manifest stays the source of truth, so a
    transiently-stale on-disk write is self-corrected by the next save."""
    st = get_storage(cfg)
    with _MANIFEST_LOCK:
        data = manifest.to_json()
    st.write_text(M.manifest_path(st, manifest.output_root), data)


# ── Persisted thumbnails ─────────────────────────────────────────────────
# Board/gallery speed: instead of downloading every full-res 2K image and
# decoding it on first paint, we persist a small JPEG (~512px, ~30KB) next to the
# outputs under `<output_root>/.thumbs/<sha>.jpg` whenever an image is written. The
# board reads those tiny files; full-res is fetched only on demand (detail/zoom).
THUMB_MAX_EDGE = 512
THUMB_QUALITY = 80


def thumb_storage_path(st: Storage, output_root: str, src_path: str) -> str:
    """Stable storage path for the small thumbnail of `src_path`."""
    key = hashlib.sha256(str(src_path).encode("utf-8")).hexdigest()[:20]
    return st.join(output_root, ".thumbs", f"{key}.jpg")


def write_thumb(st: Storage, output_root: str, src_path: str, src_bytes: bytes,
                max_edge: int = THUMB_MAX_EDGE) -> Optional[str]:
    """Encode + persist a small JPEG thumbnail of `src_bytes`. Returns the thumb
    storage path, or None on failure (never raises — thumbnails are best-effort)."""
    try:
        import io
        from PIL import Image as _PI
        img = _PI.open(io.BytesIO(src_bytes)).convert("RGB")
        if max(img.size) > max_edge:
            s = max_edge / max(img.size)
            img = img.resize((int(img.size[0] * s), int(img.size[1] * s)), _PI.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=THUMB_QUALITY, optimize=True)
        tp = thumb_storage_path(st, output_root, src_path)
        st.write_bytes(tp, buf.getvalue())
        return tp
    except Exception:
        return None


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
    """Return [(product_name, image_path)] for every image in brand_root/<product>/.

    On the Dropbox backend each `list_files` is a network round-trip, so the per-
    product listings are fanned out across a thread pool — turning N sequential
    calls into a handful of concurrent batches. `ThreadPoolExecutor.map` preserves
    input order, so the result order is identical to the sequential version."""
    st = get_storage(cfg)
    if not st.exists(brand_root):
        return []
    product_dirs = st.list_subdirs(brand_root)

    def _scan(product_dir: str) -> list[tuple[str, str]]:
        product = st.name(product_dir)
        return [
            (product, img)
            for img in st.list_files(product_dir)
            if Path(st.name(img)).suffix.lower() in IMAGE_EXTS
        ]

    out: list[tuple[str, str]] = []
    if getattr(st, "backend", "local") == "dropbox" and len(product_dirs) > 1:
        with ThreadPoolExecutor(max_workers=8) as ex:
            for res in ex.map(_scan, product_dirs):
                out.extend(res)
    else:
        for pd in product_dirs:
            out.extend(_scan(pd))
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
    force_rescan: bool = False,
) -> M.Manifest:
    """Build (or top-up) manifest from the brand folder.

    If `project` is supplied, its per-product briefs and classifications are written
    into the manifest, and new photos get their `classification` and `brief_notes`
    pre-filled from the matching ProductGroup — no Claude vision call needed.

    Folder discovery is cached on the manifest (`discovered`). A normal open reuses
    that cache (no backend listing); pass `force_rescan=True` (the "Rescan folder"
    action) to re-list and pick up newly-added renders.
    """
    st = get_storage(cfg)
    brand_root = str(brand_root)
    output_root = str(output_root)

    existing = M.load(output_root, st)
    if existing and existing.brand_root == brand_root:
        manifest = existing
        dirty = False
    else:
        manifest = M.Manifest(brand_root=brand_root, output_root=output_root)
        dirty = True

    # If a project is provided, sync its briefs + classifications into the manifest.
    # This keeps the manifest a self-contained source of truth (so the pipeline can
    # run even if the Project JSON is deleted). Only mark dirty when something actually
    # changed, so re-opening an unchanged project doesn't trigger a manifest re-upload.
    if project is not None:
        briefs = project.product_brief_map()
        classes = project.product_classification_map()
        if (manifest.project_slug != project.slug
                or manifest.product_briefs != briefs
                or manifest.product_classifications != classes):
            manifest.project_slug = project.slug
            manifest.product_briefs = briefs
            manifest.product_classifications = classes
            dirty = True

    # Discovery: reuse the cached list unless asked to rescan (listing every product
    # folder on Dropbox is the slow part of opening). Cache is [[product, path], ...].
    if manifest.discovered and not force_rescan:
        discovered = [(p, img) for p, img in manifest.discovered]
    else:
        discovered = discover_images(cfg, brand_root)
        new_cache = [[p, img] for p, img in discovered]
        if new_cache != manifest.discovered:
            manifest.discovered = new_cache
            dirty = True

    for product, img in discovered:
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
            dirty = True

    # Reconcile transient variants WITHOUT any network calls. Variants are persisted
    # under `<output_root>/.variants/...` (see generate_variants_for). Older manifests
    # (pre-persistence) recorded paths in an ephemeral local scratch dir — e.g. a prior
    # cloud container's /tmp — that no longer exists. Those legacy paths are simply the
    # ones NOT under the storage variants root, so a pure prefix check identifies them
    # — no per-photo `st.exists` probe (which on Dropbox was one round-trip per photo,
    # a major chunk of the open-time). Files under .variants are trusted, exactly as the
    # board/detail display already trusts the manifest (missing files fall back cleanly).
    variants_root = st.join(output_root, ".variants").replace("\\", "/")
    for photo in manifest.photos.values():
        if photo.variants:
            first = str(photo.variants[0]).replace("\\", "/")
            if not first.startswith(variants_root):
                photo.variants = []
                photo.selected_variant = None
                dirty = True

    # Only touch storage when something actually changed. A no-op re-open then costs
    # just the manifest read + the (parallel) folder scan — no makedirs, no re-upload.
    if dirty:
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


def _prepare_worn_prompt(
    cfg: Config,
    manifest: M.Manifest,
    photo: M.PhotoState,
    anthropic_client: AnthropicClient,
    materials_description: str,
) -> None:
    """Parametrized worn-render prompt in two stages:

      1. ANALYZER — Claude analyzes the render once → a *templated* prompt with the
         six styling {PLACEHOLDER}s left literal. Cached on photo.worn_template;
         the API call runs only when the template is missing.
      2. ASSEMBLER — fill the placeholders from photo.worn_params (randomized at
         first prepare). Instant, no API cost — so changing a styling parameter only
         re-assembles, never re-analyzes.
    """
    st = get_storage(cfg)

    # Run the (slow) analyzer call OUTSIDE the lock; only mutate + persist after.
    res = None
    if not photo.worn_template:
        res = anthropic_client.analyze_worn(
            Path(st.materialize(photo.input_path)),
            system_prompt=cfg.get_worn_analyzer(),
            materials_description=(materials_description or photo.brief_notes or ""),
        )
    truncated = bool(res is not None and res.stop_reason == "max_tokens")

    # One short mutation under the lock, then a single persist outside it.
    with _MANIFEST_LOCK:
        if res is not None:
            photo.worn_template = res.text
            photo.cost_usd += res.cost_usd
            manifest.total_cost_usd += res.cost_usd
        # Randomize styling parameters on the first prepare (auto-prepare requirement).
        if not photo.worn_params:
            photo.worn_params = WP.random_params()
        photo.prompt = WP.assemble(photo.worn_template, photo.worn_params)
        if truncated:
            photo.last_error = (
                "⚠ Worn analyzer output truncated — raise anthropic.analyzer_max_tokens in config.yaml."
            )
        elif photo.last_error and "truncated" in (photo.last_error or ""):
            photo.last_error = None
    _persist(cfg, manifest)


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
    # Per-product description (set at project creation), prepended to the brief.
    product_description = manifest.product_briefs.get(photo.product, "")

    # Worn shots use the parametrized analyzer + assembler. On any failure we fall
    # back to the legacy brief-based prompt below so generation never hard-fails.
    if classification == "worn":
        try:
            _prepare_worn_prompt(cfg, manifest, photo, anthropic_client, product_description)
            return
        except Exception as e:
            with _MANIFEST_LOCK:
                photo.last_error = f"Worn analyzer failed ({e}); used legacy brief."
            _persist(cfg, manifest)
            # fall through to the legacy brief path

    # Packshot shots use ONLY the input 3D render as reference (Image 1).
    # Worn shots (legacy fallback) use Image 2/3/4 from prompts/product_refs/<product>/.
    refs = cfg.get_product_refs(photo.product) if classification == "worn" else {}
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
    _persist(cfg, manifest)


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
        write_thumb(st, manifest.output_root, vp, r.image_bytes)  # persist a fast board thumbnail
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
    _persist(cfg, manifest)


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
            _persist(cfg, manifest)
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
    For worn shots it also clears the cached analyzer template, so "(Re)generate prompt"
    means a genuinely fresh analysis (not just re-assembling the same template).
    """
    def _task():
        try:
            if photo.classification is None:
                classify_photo(cfg, manifest, photo, anthropic_client)
            if force:
                with _MANIFEST_LOCK:
                    photo.prompt = None
                    if (photo.classification or "packshot") == "worn":
                        photo.worn_template = None
            generate_prompt_for(cfg, manifest, photo, anthropic_client)
        except Exception as e:
            with _MANIFEST_LOCK:
                photo.last_error = str(e)
            _persist(cfg, manifest)
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
    write_thumb(st, manifest.output_root, photo.output_path, graded)  # refresh the board thumbnail

    with _MANIFEST_LOCK:
        photo.selected_variant = variant_path
        photo.graded = True
    _persist(cfg, manifest)


# ── Gallery model: keep-all variants, soft-delete, grade per variant ──────

def _graded_output_path(st: Storage, manifest: M.Manifest,
                        photo: M.PhotoState, variant_path: str) -> str:
    """Per-variant graded deliverable path under <product>/renders/."""
    stem = st.stem(photo.input_path)
    vname = st.stem(variant_path)   # e.g. variant_001
    return st.join(manifest.output_root, photo.product, "renders",
                   f"{stem}__{vname}__graded.png")


def grade_variant(cfg: Config, manifest: M.Manifest,
                  photo: M.PhotoState, variant_path: str) -> str:
    """Grade ONE kept variant against the relevant hero and write a per-variant
    deliverable to <product>/renders/. Records it in photo.graded_variants. Unlike
    the legacy select_variant, this does not single-select — many variants per photo
    can be graded and kept. Returns the graded output path."""
    if variant_path not in photo.variants:
        raise ValueError(f"variant_path {variant_path} not in variants for {photo.photo_id}")
    st = get_storage(cfg)
    src_bytes = st.read_bytes(variant_path)

    hero_rgb = None
    hero_resolved = hero_path_for_photo(cfg, manifest, photo)
    if hero_resolved:
        import numpy as np
        from PIL import Image as _PI
        hero_img = _PI.open(st.materialize(hero_resolved)).convert("RGB")
        hero_rgb = np.array(hero_img)

    graded = grade_image(src_bytes, photo.classification or "packshot", hero_rgb=hero_rgb)
    out = _graded_output_path(st, manifest, photo, variant_path)
    st.write_bytes(out, graded)
    write_thumb(st, manifest.output_root, out, graded)

    with _MANIFEST_LOCK:
        photo.graded_variants[variant_path] = out
        photo.graded = True            # legacy: "has at least one graded"
        photo.selected_variant = variant_path
    _persist(cfg, manifest)
    return out


def submit_grade_variant(cfg: Config, manifest: M.Manifest,
                         photo: M.PhotoState, variant_path: str) -> Future:
    """Grade one variant in the background grading pool."""
    def _task():
        try:
            grade_variant(cfg, manifest, photo, variant_path)
        except Exception as e:
            with _MANIFEST_LOCK:
                photo.last_error = f"Grading failed: {e}"
            _persist(cfg, manifest)
            raise
    return _get_grade_executor().submit(_task)


def hide_variant(cfg: Config, manifest: M.Manifest,
                 photo: M.PhotoState, variant_path: str) -> None:
    """Soft-delete a variant: hide it from the gallery (file kept, reversible)."""
    with _MANIFEST_LOCK:
        if variant_path not in photo.hidden_variants:
            photo.hidden_variants.append(variant_path)
    _persist(cfg, manifest)


def unhide_variant(cfg: Config, manifest: M.Manifest,
                   photo: M.PhotoState, variant_path: str) -> None:
    """Undo a soft-delete."""
    with _MANIFEST_LOCK:
        photo.hidden_variants = [v for v in photo.hidden_variants if v != variant_path]
    _persist(cfg, manifest)


def purge_hidden(cfg: Config, manifest: M.Manifest) -> int:
    """Permanently delete soft-deleted variants (and their graded outputs + thumbs).
    Returns the count purged."""
    st = get_storage(cfg)
    n = 0
    for photo in manifest.photos.values():
        for v in list(photo.hidden_variants or []):
            graded = (photo.graded_variants or {}).get(v)
            for path in [p for p in (v, graded) if p]:
                for target in (path, thumb_storage_path(st, manifest.output_root, path)):
                    try:
                        st.delete(target)
                    except Exception:
                        pass
            with _MANIFEST_LOCK:
                photo.variants = [x for x in photo.variants if x != v]
                photo.graded_variants.pop(v, None)
            n += 1
        with _MANIFEST_LOCK:
            photo.hidden_variants = []
    _persist(cfg, manifest)
    return n


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
            _persist(cfg, manifest)
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
