"""Claude API: classify image (packshot/worn) + generate Nano Banana prompt."""

from __future__ import annotations

import base64
import io
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import anthropic
from PIL import Image, ImageOps

from .config import Config

# Anthropic accepts up to ~5MB per image and recommends resizing to ≤1568px on the long edge.
_MAX_EDGE = 1568


@dataclass
class ClaudeCallResult:
    text: str
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    cost_usd: float
    stop_reason: str = ""    # "end_turn" = clean, "max_tokens" = truncated


def _b64_image(image_path: Path) -> tuple[str, str]:
    """
    Return (media_type, base64_data) for an image.

    Decoded with PIL and re-encoded as PNG so the media_type ALWAYS matches the actual bytes.
    Resized to ≤_MAX_EDGE on the long edge to keep payloads under Anthropic's 5MB limit
    and per their docs' recommendation. EXIF orientation respected.
    """
    img = Image.open(image_path)
    img = ImageOps.exif_transpose(img).convert("RGB")
    if max(img.size) > _MAX_EDGE:
        scale = _MAX_EDGE / max(img.size)
        new_size = (int(round(img.size[0] * scale)), int(round(img.size[1] * scale)))
        img = img.resize(new_size, Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    data = base64.standard_b64encode(buf.getvalue()).decode("ascii")
    return "image/png", data


def _compute_cost(cfg: Config, in_tok: int, out_tok: int, cache_read: int, cache_creation: int = 0) -> float:
    # Cache creation is billed at 1.25× input rate for ephemeral cache
    cache_creation_per_mtok = cfg.anthropic_cost_input_per_mtok * 1.25
    return (
        (in_tok / 1_000_000) * cfg.anthropic_cost_input_per_mtok
        + (out_tok / 1_000_000) * cfg.anthropic_cost_output_per_mtok
        + (cache_read / 1_000_000) * cfg.anthropic_cost_cache_read_per_mtok
        + (cache_creation / 1_000_000) * cache_creation_per_mtok
    )


class AnthropicClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        if not cfg.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY missing — add it to _auto_pipeline/.env")
        self.client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)

    def classify(self, image_path: Path) -> ClaudeCallResult:
        """Single-word classification: 'packshot' or 'worn'."""
        media_type, b64 = _b64_image(image_path)
        resp = self.client.messages.create(
            model=self.cfg.anthropic_model,
            max_tokens=self.cfg.anthropic_classify_max_tokens,
            system=[
                {
                    "type": "text",
                    "text": self.cfg.classifier_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {"type": "base64", "media_type": media_type, "data": b64},
                        },
                    ],
                }
            ],
        )
        text = "".join(block.text for block in resp.content if block.type == "text").strip().lower()
        # Defensive parsing — accept first matching keyword
        if "packshot" in text:
            text = "packshot"
        elif "worn" in text:
            text = "worn"
        else:
            text = "packshot"  # default fallback

        u = resp.usage
        cost = _compute_cost(
            self.cfg,
            in_tok=u.input_tokens,
            out_tok=u.output_tokens,
            cache_read=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_creation=getattr(u, "cache_creation_input_tokens", 0) or 0,
        )
        return ClaudeCallResult(
            text=text,
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cache_creation_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
            cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
            cost_usd=cost,
            stop_reason=getattr(resp, "stop_reason", "") or "",
        )

    def analyze_worn(
        self,
        image_path: Path,
        system_prompt: str,
        materials_description: str = "",
        max_tokens: int | None = None,
    ) -> ClaudeCallResult:
        """Worn-render ANALYZER. Send the 3D render + the parametrized analyzer system
        prompt; receive a *templated* Nano Banana prompt with the six styling
        {PLACEHOLDER}s left literal (the app fills them later via the assembler).

        Returns the templated prompt with any surrounding ``` fences stripped.
        """
        if not system_prompt or not system_prompt.strip():
            raise RuntimeError("Worn analyzer system prompt missing (prompts/worn_analyzer.md).")

        media_type, b64 = _b64_image(image_path)
        user_text = (
            f'Jewelry materials description: "{materials_description.strip()}". Generate the templated prompt.'
            if materials_description and materials_description.strip()
            else "No materials description provided — infer from the render. Generate the templated prompt."
        )
        resp = self.client.messages.create(
            model=self.cfg.anthropic_model,
            max_tokens=max_tokens or self.cfg.anthropic_analyzer_max_tokens,
            temperature=0.2,
            system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                    {"type": "text", "text": user_text},
                ],
            }],
        )
        text = "".join(block.text for block in resp.content if block.type == "text").strip()
        # Strip a surrounding fenced code block if Claude wrapped the prompt.
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines).strip()

        u = resp.usage
        cost = _compute_cost(
            self.cfg,
            in_tok=u.input_tokens,
            out_tok=u.output_tokens,
            cache_read=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_creation=getattr(u, "cache_creation_input_tokens", 0) or 0,
        )
        return ClaudeCallResult(
            text=text,
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cache_creation_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
            cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
            cost_usd=cost,
            stop_reason=getattr(resp, "stop_reason", "") or "",
        )

    def refine_prompt(
        self,
        image_path: Path,
        current_text: str,
        feedback: str,
        system_prompt: str,
        ref_image_paths: list[Path] | None = None,
        max_tokens: int | None = None,
    ) -> ClaudeCallResult:
        """Revise an existing prompt (or worn template) per the art director's feedback.
        Sends the render (Image 1) + optional AD reference images (Image 2+) + the
        current prompt + the feedback. Returns the revised prompt, fences stripped."""
        content: list[dict] = []
        m1, b1 = _b64_image(image_path)
        content.append({"type": "image", "source": {"type": "base64", "media_type": m1, "data": b1}})
        content.append({"type": "text", "text": "[Image 1] above — the 3D render."})
        for i, rp in enumerate(ref_image_paths or [], start=2):
            try:
                m, b = _b64_image(rp)
            except Exception:
                continue
            content.append({"type": "image", "source": {"type": "base64", "media_type": m, "data": b}})
            content.append({"type": "text", "text": f"[Image {i}] above — art-director reference."})
        content.append({"type": "text", "text":
            f"CURRENT PROMPT:\n```\n{current_text}\n```\n\n"
            f"ART DIRECTOR FEEDBACK:\n{feedback.strip()}\n\n"
            f"Produce the revised prompt now, following the system instructions exactly."})

        resp = self.client.messages.create(
            model=self.cfg.anthropic_model,
            max_tokens=max_tokens or self.cfg.anthropic_analyzer_max_tokens,
            temperature=0.3,
            system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": content}],
        )
        text = "".join(block.text for block in resp.content if block.type == "text").strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        u = resp.usage
        cost = _compute_cost(
            self.cfg, in_tok=u.input_tokens, out_tok=u.output_tokens,
            cache_read=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_creation=getattr(u, "cache_creation_input_tokens", 0) or 0,
        )
        return ClaudeCallResult(
            text=text, input_tokens=u.input_tokens, output_tokens=u.output_tokens,
            cache_creation_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
            cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
            cost_usd=cost, stop_reason=getattr(resp, "stop_reason", "") or "",
        )

    def generate_prompt(
        self,
        image_path: Path,
        classification: str,
        refs: dict[int, Path] | None = None,
        brief_notes: str = "",
        product_description: str = "",
    ) -> ClaudeCallResult:
        """
        Use the classification-based brief as system prompt.
        User message includes:
          [Image 1] = image_path (the input 3D render)
          [Image 2..4] = any per-product reference images (from refs dict, keyed by slot number)
          brief_notes = optional short PER-PHOTO note from the user (overrides product_description if set)
          product_description = PER-PRODUCT description set at project creation
                                (e.g. "a ring in gold and diamonds"). Prepended as the
                                first line of the system prompt so Claude doesn't make
                                vision mistakes about the jewelry type/material.
        """
        brief = self.cfg.get_brief(classification)
        if not brief or not brief.strip():
            raise RuntimeError(
                f"Brief missing for classification '{classification}'. "
                f"Add _auto_pipeline/prompts/brief_{classification}.md"
            )

        # If we have a per-product description, prepend it to the brief as a hard
        # ground-truth statement Claude reads BEFORE its long instruction list.
        # This stops Claude from inventing materials/cuts that aren't in the render.
        if product_description and product_description.strip():
            brief = (
                f'The image attached is: "{product_description.strip()}"\n\n'
                f"---\n\n"
                f"{brief}"
            )

        content: list[dict] = []

        # [Image 1] — input render
        m1, b1 = _b64_image(image_path)
        content.append({"type": "image", "source": {"type": "base64", "media_type": m1, "data": b1}})
        content.append({"type": "text", "text": "[Image 1] above — the 3D render (pose/geometry/composition reference)."})

        # [Image 2..4] — product references
        for slot, ref_path in sorted((refs or {}).items()):
            m, b = _b64_image(ref_path)
            content.append({"type": "image", "source": {"type": "base64", "media_type": m, "data": b}})
            content.append({"type": "text", "text": f"[Image {slot}] above — product reference (slot {slot})."})

        # Final instruction — includes user's brief notes if provided
        final_text = f"Classification: {classification}."
        if brief_notes and brief_notes.strip():
            final_text += f"\n\nUser brief: {brief_notes.strip()}"
        final_text += "\n\nWrite the Nano Banana Pro prompt now, following the system instructions exactly."
        content.append({"type": "text", "text": final_text})

        resp = self.client.messages.create(
            model=self.cfg.anthropic_model,
            max_tokens=self.cfg.anthropic_prompt_max_tokens,
            system=[
                {"type": "text", "text": brief, "cache_control": {"type": "ephemeral"}}
            ],
            messages=[{"role": "user", "content": content}],
        )
        text = "".join(block.text for block in resp.content if block.type == "text").strip()
        # Strip surrounding quotes if Claude added them
        if text.startswith(('"', "'")) and text.endswith(('"', "'")) and len(text) > 2:
            text = text[1:-1].strip()

        u = resp.usage
        cost = _compute_cost(
            self.cfg,
            in_tok=u.input_tokens,
            out_tok=u.output_tokens,
            cache_read=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_creation=getattr(u, "cache_creation_input_tokens", 0) or 0,
        )
        return ClaudeCallResult(
            text=text,
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cache_creation_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
            cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
            cost_usd=cost,
            stop_reason=getattr(resp, "stop_reason", "") or "",
        )
