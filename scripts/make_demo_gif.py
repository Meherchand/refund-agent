#!/usr/bin/env python3
"""Render the six-tab demo storyboard as docs/demo.gif.

This is the L8 demo artifact — a narratable storyboard, not a screen recording.
Re-run after changing the beat list: `uv run --with pillow scripts/make_demo_gif.py`
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "demo.gif"

# Six surfaces (Langfuse declined — /review already shows the spans a trace would).
BEATS = [
    ("1 / 6  Customer form", ":8000", "Submit a wardrobing return.\nCase lands in RECEIVED."),
    ("2 / 6  RabbitMQ", ":15672", "Message on case-events.\nWorker acks; DLQ stays empty."),
    ("3 / 6  MinIO", ":9201", "Frozen EvidenceBundle\nbundles/{case}/{hash}.json"),
    ("4 / 6  Reviewer dashboard", ":8000/review", "Queue shows binding rule.\nCritic dissent + gate input."),
    ("5 / 6  OPA", ":8281", "Paste GateInput → ESCALATE.\nrule_id = single_use_goods"),
    ("6 / 6  MCP access log", "psql", "SELECT tool, ok FROM\nmcp_access_log — INSERT-only."),
]

W, H = 960, 540
BG, PANEL, INK, MUTED, ACCENT = "#0f172a", "#1e293b", "#f8fafc", "#94a3b8", "#38bdf8"


def font(size: int) -> ImageFont.ImageFont:
    for name in (
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def frame(title: str, port: str, body: str) -> Image.Image:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((40, 40, W - 40, H - 40), radius=24, fill=PANEL)
    d.text((72, 72), "Governed Refund Agents — demo", fill=MUTED, font=font(22))
    d.text((72, 120), title, fill=INK, font=font(40))
    d.text((72, 180), port, fill=ACCENT, font=font(28))
    d.multiline_text((72, 260), body, fill=INK, font=font(32), spacing=12)
    d.text((72, H - 100), "LLM gathers evidence · OPA decides · humans override",
           fill=MUTED, font=font(20))
    return img


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    frames = [frame(*b) for b in BEATS]
    frames[0].save(OUT, save_all=True, append_images=frames[1:], duration=1800,
                   loop=0, optimize=False)
    print(f"wrote {OUT} ({len(frames)} frames)")


if __name__ == "__main__":
    main()
