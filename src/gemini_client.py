"""Nano Banana Pro (Gemini 3 image) client — single + N-parallel generation."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from google import genai
from google.genai import types

from .config import Config


@dataclass
class GeminiResult:
    image_bytes: bytes
    cost_usd: float


@dataclass
class GeminiBatchResult:
    results: list[Optional[GeminiResult]]  # one entry per requested variant, None if failed
    cost_usd: float
    errors: list[str]


def _read_reference(reference_path: Path) -> tuple[bytes, str]:
    import mimetypes
    data = reference_path.read_bytes()
    mime, _ = mimetypes.guess_type(str(reference_path))
    if not mime or not mime.startswith("image/"):
        mime = "image/png"
    return data, mime


def _extract_image_bytes(response) -> Optional[bytes]:
    """Walk the response and return the first image's PNG bytes (or None)."""
    for cand in getattr(response, "candidates", []) or []:
        content = getattr(cand, "content", None)
        for part in getattr(content, "parts", []) or []:
            inline = getattr(part, "inline_data", None)
            if inline is not None and getattr(inline, "data", None):
                return inline.data
    # newer SDK exposes response.parts directly
    for part in getattr(response, "parts", []) or []:
        inline = getattr(part, "inline_data", None)
        if inline is not None and getattr(inline, "data", None):
            return inline.data
    return None


class GeminiClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        if not cfg.gemini_api_key:
            raise RuntimeError("GEMINI_API_KEY missing — add it to _auto_pipeline/.env")
        self.client = genai.Client(api_key=cfg.gemini_api_key)

    def generate_one(
        self,
        prompt: str,
        input_reference: Path,
        additional_refs: list[Path] | None = None,
        aspect_ratio: str | None = None,
    ) -> GeminiResult:
        """
        Generate one image. References are passed in this order:
          [Image 1] = input_reference (the 3D render)
          [Image 2..N] = additional_refs (per-product reference images)
        """
        parts: list = []

        # [Image 1] — the input render
        b1, m1 = _read_reference(input_reference)
        parts.append(types.Part.from_bytes(data=b1, mime_type=m1))

        # [Image 2..N] — extra refs (capped at Gemini's 14-image total)
        for ref in (additional_refs or [])[:13]:
            rb, rm = _read_reference(ref)
            parts.append(types.Part.from_bytes(data=rb, mime_type=rm))

        # Trailing text prompt
        parts.append(prompt)

        last_exc: Optional[Exception] = None
        for attempt in range(self.cfg.gemini_max_retries):
            try:
                response = self.client.models.generate_content(
                    model=self.cfg.gemini_model,
                    contents=parts,
                    config=types.GenerateContentConfig(
                        response_modalities=["IMAGE"],
                        image_config=types.ImageConfig(
                            aspect_ratio=aspect_ratio or self.cfg.gemini_aspect_ratio,
                            image_size=self.cfg.gemini_image_size,
                        ),
                    ),
                )
                img = _extract_image_bytes(response)
                if img is None:
                    raise RuntimeError("Gemini returned no image data")
                return GeminiResult(image_bytes=img, cost_usd=self.cfg.effective_cost_per_image)
            except Exception as e:
                last_exc = e
                if attempt < self.cfg.gemini_max_retries - 1:
                    time.sleep(2 ** (attempt + 1))
        raise RuntimeError(f"Gemini call failed after {self.cfg.gemini_max_retries} attempts: {last_exc}")

    def generate_n(
        self,
        prompt: str,
        input_reference: Path,
        n: int,
        additional_refs: list[Path] | None = None,
        aspect_ratio: str | None = None,
        max_concurrency: int = 4,
    ) -> GeminiBatchResult:
        """Generate N variants in parallel. Failures captured per-slot."""
        results: list[Optional[GeminiResult]] = [None] * n
        errors: list[str] = []
        total_cost = 0.0

        def run_one():
            return self.generate_one(prompt, input_reference, additional_refs, aspect_ratio)

        with ThreadPoolExecutor(max_workers=min(max_concurrency, n)) as ex:
            futures = {ex.submit(run_one): i for i in range(n)}
            for fut in as_completed(futures):
                idx = futures[fut]
                try:
                    results[idx] = fut.result()
                    total_cost += results[idx].cost_usd
                except Exception as e:
                    errors.append(f"variant {idx}: {e}")
        return GeminiBatchResult(results=results, cost_usd=total_cost, errors=errors)
