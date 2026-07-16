"""Génération de PDF imprimables pour pochettes et tray cards.

Dimensions standardisées jewel case CD :
- Front booklet (cover) : 120 × 120 mm
- Back tray card         : 150 × 118 mm (dont 2 spines de 6,5 mm)

Le PDF est au format A4 portrait, l'image centrée avec traits de coupe
et repères de pliage (tray card). Imprimé à 100 %, les dimensions
correspondent au mm près.
"""
from __future__ import annotations

import io
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen.canvas import Canvas

COVER_W, COVER_H = 120 * mm, 120 * mm
TRAY_W, TRAY_H = 150 * mm, 118 * mm
SPINE_W = 6.5 * mm
CROP_LEN = 8 * mm
CROP_OFFSET = 3 * mm

A4_W, A4_H = A4


def _crop_marks(c: Canvas, x: float, y: float, w: float, h: float) -> None:
    c.setStrokeColorRGB(0, 0, 0)
    c.setLineWidth(0.3)
    for cx, cy, segs in [
        (x, y, [(-CROP_LEN, 0, -CROP_OFFSET, 0), (0, -CROP_LEN, 0, -CROP_OFFSET)]),
        (x + w, y, [(CROP_OFFSET, 0, CROP_LEN, 0), (0, -CROP_LEN, 0, -CROP_OFFSET)]),
        (x, y + h, [(-CROP_LEN, 0, -CROP_OFFSET, 0), (0, CROP_OFFSET, 0, CROP_LEN)]),
        (x + w, y + h, [(CROP_OFFSET, 0, CROP_LEN, 0), (0, CROP_OFFSET, 0, CROP_LEN)]),
    ]:
        for dx1, dy1, dx2, dy2 in segs:
            c.line(cx + dx1, cy + dy1, cx + dx2, cy + dy2)


def _fold_marks(c: Canvas, x: float, y: float, h: float, spine: float) -> None:
    """Repères de pliage pour les spines (traits pointillés verticaux)."""
    c.setStrokeColorRGB(0.6, 0.6, 0.6)
    c.setLineWidth(0.25)
    c.setDash(2, 2)
    for sx in (x + spine, x + TRAY_W - spine):
        c.line(sx, y - CROP_OFFSET, sx, y)
        c.line(sx, y + h, sx, y + h + CROP_OFFSET)
    c.setDash()


def cover_pdf(image_path: Path) -> bytes:
    buf = io.BytesIO()
    c = Canvas(buf, pagesize=A4)

    x = (A4_W - COVER_W) / 2
    y = (A4_H - COVER_H) / 2

    c.drawImage(str(image_path), x, y, COVER_W, COVER_H,
                preserveAspectRatio=True, anchor="c")
    _crop_marks(c, x, y, COVER_W, COVER_H)

    c.setFont("Helvetica", 7)
    c.setFillColorRGB(0.5, 0.5, 0.5)
    c.drawCentredString(A4_W / 2, y - 14 * mm,
                        "FRONT BOOKLET — 120 × 120 mm — PRINT AT 100 %")

    c.save()
    return buf.getvalue()


def traycard_pdf(image_path: Path) -> bytes:
    buf = io.BytesIO()
    c = Canvas(buf, pagesize=A4)

    x = (A4_W - TRAY_W) / 2
    y = (A4_H - TRAY_H) / 2

    c.drawImage(str(image_path), x, y, TRAY_W, TRAY_H,
                preserveAspectRatio=True, anchor="c")
    _crop_marks(c, x, y, TRAY_W, TRAY_H)
    _fold_marks(c, x, y, TRAY_H, SPINE_W)

    c.setFont("Helvetica", 7)
    c.setFillColorRGB(0.5, 0.5, 0.5)
    c.drawCentredString(
        A4_W / 2, y - 14 * mm,
        "JEWEL CASE TRAY CARD — 150 × 118 mm — "
        "FOLD the two 6.5 mm spines — PRINT AT 100 %")

    c.save()
    return buf.getvalue()
