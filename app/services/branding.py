"""Per-shop branding of the public order form: accent colour and logo.

The accent colour is turned into a small palette (hover, soft background, text shade and
the text colour that goes on top of it) chosen for WCAG contrast, then served as a
stylesheet (`/form/<slug>/theme.css`) because the Content-Security-Policy forbids inline
styles. Logos are stored in the database (the production container is read-only) and only
PNG / JPEG / WebP are accepted (no SVG: it can carry scripts).
"""

from __future__ import annotations

import re

HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")
MAX_LOGO_BYTES = 300_000
LOGO_TYPES = {"image/png", "image/jpeg", "image/webp"}


class BrandingError(ValueError):
    pass


def normalize_hex(value: str) -> str:
    m = HEX_RE.match(value.strip())
    if not m:
        raise BrandingError("couleur invalide (format #RRGGBB)")
    return "#" + m.group(1).lower()


def _rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _hex(rgb: tuple[float, float, float]) -> str:
    return "#" + "".join(f"{max(0, min(255, round(c))):02x}" for c in rgb)


def luminance(hex_color: str) -> float:
    def channel(c: int) -> float:
        s = c / 255
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in _rgb(hex_color))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def mix(a: str, b: str, weight: float) -> str:
    """`weight` of colour a, the rest of colour b."""
    ra, rb = _rgb(a), _rgb(b)
    return _hex(tuple(x * weight + y * (1 - weight) for x, y in zip(ra, rb, strict=True)))


def darken_until(color: str, background: str, target: float) -> str:
    """Darken `color` towards black until it reaches `target` contrast on `background`."""
    shade = color
    for step in range(0, 101, 5):
        shade = mix(color, "#000000", 1 - step / 100)
        if contrast(shade, background) >= target:
            return shade
    return shade


def palette(brand: str) -> dict[str, str]:
    """Accessible palette derived from one accent colour."""
    brand = normalize_hex(brand)
    on_brand = "#ffffff" if contrast(brand, "#ffffff") >= contrast(brand, "#111111") else "#111111"
    return {
        "brand": brand,
        "brand-strong": mix(brand, "#000000", 0.85),
        "brand-soft": mix(brand, "#ffffff", 0.12),
        # Text in the accent colour (links, icons) must stay readable on white.
        "brand-ink": darken_until(brand, "#ffffff", 4.5),
        "on-brand": on_brand,
        "focus": brand + "59",  # ~35% alpha
    }


def theme_css(brand: str) -> str:
    """Stylesheet overriding the app's accent for one shop (light and dark)."""
    p = palette(brand)
    light = "".join(f"--{k}:{v};" for k, v in p.items())
    dark_palette = {
        **p,
        "brand-soft": mix(p["brand"], "#0b1018", 0.22),
        "brand-ink": mix(p["brand"], "#ffffff", 0.7),
    }
    dark = "".join(f"--{k}:{v};" for k, v in dark_palette.items())
    auto_dark = 'html:root:root:not([data-theme="light"])'
    # html:root:root beats the app's :root[data-theme] and media-query selectors.
    return (
        f"html:root:root{{{light}}}\n"
        f"@media (prefers-color-scheme: dark){{{auto_dark}{{{dark}}}}}\n"
        f'html:root:root[data-theme="dark"]{{{dark}}}\n'
        ".public a.link,.public .trust svg.icon{color:var(--brand-ink);}\n"
    )


def detect_logo_type(data: bytes) -> str:
    """Content type from the file's magic bytes (never trust the browser's)."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) > 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    raise BrandingError("format non supporté (PNG, JPEG ou WebP)")


def validate_logo(data: bytes) -> str:
    if not data:
        raise BrandingError("fichier vide")
    if len(data) > MAX_LOGO_BYTES:
        raise BrandingError("logo trop lourd (300 Ko maximum)")
    return detect_logo_type(data)
