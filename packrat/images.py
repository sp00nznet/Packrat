"""Fetch Google Play images and render them as terminal pixels.

Uses Pillow + rich-pixels (half-block characters). Rendering is best-effort:
if the optional deps are missing or a fetch fails, ``render`` returns ``None``
and the UI simply shows no image.
"""

from __future__ import annotations

import io
from typing import Optional

import httpx

try:
    from PIL import Image
    from rich_pixels import Pixels

    _AVAILABLE = True
except Exception:  # pragma: no cover - optional deps
    _AVAILABLE = False


def available() -> bool:
    return _AVAILABLE


def render(url: str, max_cols: int, max_rows: int, size_hint: int = 256):
    """Fetch *url* and fit it into ``max_cols`` x ``max_rows`` character cells.

    Returns a rich-pixels ``Pixels`` renderable, or ``None`` on any problem.
    Each character cell is ~1 column wide and 2 pixels tall (half-blocks).
    """
    if not _AVAILABLE or not url or max_cols < 1 or max_rows < 1:
        return None
    try:
        resp = httpx.get(f"{url}=s{size_hint}", timeout=30, follow_redirects=True)
        resp.raise_for_status()
        img = Image.open(io.BytesIO(resp.content)).convert("RGBA")
        # Flatten transparency onto white so icons don't render on black.
        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        img = Image.alpha_composite(bg, img).convert("RGB")

        box_w, box_h = max_cols, max_rows * 2
        iw, ih = img.size
        scale = min(box_w / iw, box_h / ih)
        img = img.resize((max(1, int(iw * scale)), max(1, int(ih * scale))))
        return Pixels.from_image(img)
    except Exception:
        return None
