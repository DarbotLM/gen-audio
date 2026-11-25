"""Render a clip MP4 whose picture is the viewport's inverse-HDR bitcube.

This is an offline port of the desktop Cube viewport (``apps/desktop/src/cubeview.ts``):
the same cube JSON fields (``points_preview``, ``cube_shape_f_t``, ``bin_seconds``,
``cube_covers_s``), the same box, camera, projection, layer colours, point size and
alpha rules, grid and playhead geometry, and the same audio-time to time-bin rule
as ``getCubePlayhead``. The desktop contract suite reads these constants from
``cubeview.ts`` so the two cannot drift apart silently.

Every frame ``i`` shows the slice for ``t = i / fps``:
``bin = clamp(floor(t / bin_seconds), 0, time_bins - 1)``. The audio is muxed from
``t = 0`` with constant-frame-rate video, so the slice is within one frame of the
audio clock. The per-frame bins are written beside the MP4 as ``<name>.slices.json``.

One known difference from WebGL: points inside the playhead band are drawn on top of
the other points instead of in JSON order. Lines are drawn ``round(dpr)`` pixels wide.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

# --- Constants ported from apps/desktop/src/cubeview.ts (checked by tests) ---
BOX = (1.5, 0.85, 1.05)
CAMERA = 4.6
SCALE = 2.1
LIFT = 0.1
YAW = -0.45
PITCH = -0.55
ZOOM = 1.0
LAYER_COLORS = {
    "signal": (0.12, 0.53, 1.0),
    "tonality": (0.16, 0.87, 0.36),
    "confidence": (1.0, 0.75, 0.12),
    "quality": (0.94, 0.27, 0.56),
}
EXPECTED = ("signal", "tonality", "confidence", "quality")
CLEAR = (0.035, 0.045, 0.07)
GRID_BOX = (0.62, 0.7, 0.82, 0.55)
GRID_FAINT = (0.5, 0.58, 0.7, 0.16)
HEAD = (0.16, 0.83, 1.0, 0.9)
HEAD_PLANE = (0.16, 0.83, 1.0, 0.12)
INK = (200, 212, 230, 0.92)
BIN_INK = (41, 211, 255, 0.95)

# Video defaults: 1920x1080 is a 1280x720 CSS viewport at devicePixelRatio 1.5.
WIDTH = 1920
HEIGHT = 1080
DPR = 1.5
FPS = 25


class VideoError(RuntimeError):
    """ffmpeg or the cube JSON could not produce the clip. The message names the piece."""


@dataclass(frozen=True)
class CubeMeta:
    title: str
    inv_hdr: float | None
    points: int
    time_bins: int
    freq_bins: int
    duration_s: float
    bin_seconds: float
    covers_s: float
    revision: int | None


@dataclass(frozen=True)
class Playhead:
    seconds: float
    bin: int
    x: float
    beyond: bool


def format_clock(seconds: float) -> str:
    """``formatClock`` from cubeview.ts: m:ss, floor of whole seconds."""
    if not math.isfinite(seconds) or seconds < 0:
        return "0:00"
    whole = math.floor(seconds)
    return f"{whole // 60}:{whole % 60:02d}"


def plane_y(z: float) -> float:
    """``planeY``: layer planes stack from signal (bottom) to quality (top)."""
    return -0.9 + z * 1.8


def parse_cube(doc: dict, name: str) -> tuple[list[dict], CubeMeta]:
    """``parseCubeDoc``: the four honesty layers only, and the clock metadata."""
    points = doc.get("points_preview") if isinstance(doc.get("points_preview"), list) else []
    layers = doc.get("layers")
    names = list(layers.keys()) if isinstance(layers, dict) else [str(n) for n in layers or []]
    names = [n for n in names if n in EXPECTED]
    if not points or any(n not in names for n in EXPECTED):
        raise VideoError("cube JSON has no signal/tonality/confidence/quality preview")
    kept = [p for p in points if p.get("layer") in names]
    shape = doc.get("cube_shape_f_t") or [0, 0]
    freq_bins, time_bins = int(shape[0]), int(shape[1])
    duration = float(doc.get("duration_s") or 0.0)
    if time_bins < 2 or duration <= 0:
        raise VideoError("cube JSON has no time bins or duration")
    bin_seconds = float(doc.get("bin_seconds") or duration / time_bins)
    covers = float(doc.get("cube_covers_s") or bin_seconds * time_bins)
    title = doc.get("title") if isinstance(doc.get("title"), str) and doc.get("title") else f"Inverse-HDR cube \u2014 {name}"
    inv = doc.get("inv_hdr")
    rev = doc.get("cube_revision")
    return kept, CubeMeta(
        title=title,
        inv_hdr=float(inv) if isinstance(inv, (int, float)) else None,
        points=len(kept),
        time_bins=time_bins,
        freq_bins=freq_bins,
        duration_s=duration,
        bin_seconds=bin_seconds,
        covers_s=covers,
        revision=int(rev) if isinstance(rev, int) else None,
    )


def playhead(seconds: float, meta: CubeMeta) -> Playhead:
    """``getCubePlayhead``: audio seconds to cube time bin, clamped, never stretched."""
    raw = math.floor(seconds / meta.bin_seconds)
    b = max(0, min(meta.time_bins - 1, raw))
    return Playhead(seconds, b, b / (meta.time_bins - 1), seconds > meta.covers_s + 1e-6)


def frame_count(duration_s: float, fps: int) -> int:
    return max(1, math.ceil(duration_s * fps - 1e-9))


def frame_bins(meta: CubeMeta, fps: int) -> list[int]:
    """The slice bin each video frame shows (frame i is t = i / fps)."""
    return [playhead(i / fps, meta).bin for i in range(frame_count(meta.duration_s, fps))]


class Projector:
    """``project`` / the ``place`` vertex shader, in buffer pixels."""

    def __init__(self, width: int, height: int):
        self.width = width
        self.height = height
        self.aspect = width / height

    def __call__(self, x: float, y: float, z: float) -> tuple[float, float]:
        cy, sy = math.cos(YAW), math.sin(YAW)
        cx, sx = math.cos(PITCH), math.sin(PITCH)
        x *= BOX[0]
        y *= BOX[1]
        z *= BOX[2]
        x1 = x * cy - z * sy
        z1 = x * sy + z * cy
        y2 = y * cx - z1 * sx
        z2 = y * sx + z1 * cx
        w = max(0.2, CAMERA + z2)
        nx = (x1 * ZOOM * SCALE) / w
        ny = (y2 * ZOOM * SCALE) / w
        if self.aspect > 1:
            nx /= self.aspect
        else:
            ny *= self.aspect
        ny += LIFT
        return ((nx + 1) / 2) * self.width, ((1 - ny) / 2) * self.height


def _font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    names = ["seguisb.ttf", "segoeuib.ttf", "DejaVuSans-Bold.ttf"] if bold else []
    names += ["segoeui.ttf", "DejaVuSans.ttf", "Arial.ttf"]
    for name in names:
        for folder in ("", "C:/Windows/Fonts/", "/usr/share/fonts/truetype/dejavu/"):
            try:
                return ImageFont.truetype(folder + name, size)
            except OSError:
                continue
    return ImageFont.load_default(size)


def _over(dst: np.ndarray, rgb: tuple[float, float, float], alpha: np.ndarray, y0: int, x0: int) -> None:
    """Premultiplied ``ONE, ONE_MINUS_SRC_ALPHA`` of one colour with a coverage*alpha mask."""
    h, w = alpha.shape
    region = dst[y0 : y0 + h, x0 : x0 + w]
    a = alpha[: region.shape[0], : region.shape[1], None]
    region *= 1.0 - a
    region += a * np.asarray(rgb, dtype=np.float32)


def _draw_segments(dst: np.ndarray, segs: list[tuple], project, width_px: int) -> None:
    """Antialiased lines ``width_px`` wide, centred on the projected segment (pixel centres at +0.5)."""
    height, width = dst.shape[:2]
    half = width_px / 2
    for a, b, rgba in segs:
        (ax, ay), (bx, by) = project(*a), project(*b)
        x0 = max(0, int(math.floor(min(ax, bx) - half - 1)))
        y0 = max(0, int(math.floor(min(ay, by) - half - 1)))
        x1 = min(width, int(math.ceil(max(ax, bx) + half + 1)) + 1)
        y1 = min(height, int(math.ceil(max(ay, by) + half + 1)) + 1)
        if x1 <= x0 or y1 <= y0:
            continue
        px = np.arange(x0, x1, dtype=np.float32)[None, :] + 0.5
        py = np.arange(y0, y1, dtype=np.float32)[:, None] + 0.5
        dx, dy = bx - ax, by - ay
        length2 = dx * dx + dy * dy
        if length2 > 0:
            u = np.clip(((px - ax) * dx + (py - ay) * dy) / length2, 0.0, 1.0)
        else:
            u = np.zeros_like(px + py)
        dist = np.hypot(px - (ax + u * dx), py - (ay + u * dy))
        cover = np.clip(half + 0.5 - dist, 0.0, 1.0).astype(np.float32) * rgba[3]
        _over(dst, rgba[:3], cover, y0, x0)


def _tick_step(bins: int, preferred: int) -> int:
    if bins <= 0:
        return 0
    return preferred * 2 if bins / preferred > 9 else preferred


def _ticks(bins: int, step: int) -> list[float]:
    if bins <= 1 or step <= 0:
        return []
    return [b / (bins - 1) for b in range(0, bins, step)]


def grid_segments(meta: CubeMeta) -> list[tuple]:
    """``gridLines``: box edges, floor and back-wall ticks, plane outlines."""
    out = []
    for y in (-1, 1):
        for z in (-1, 1):
            out.append(((-1, y, z), (1, y, z), GRID_BOX))
        for x in (-1, 1):
            out.append(((x, y, -1), (x, y, 1), GRID_BOX))
    for x in (-1, 1):
        for z in (-1, 1):
            out.append(((x, -1, z), (x, 1, z), GRID_BOX))
    for t in _ticks(meta.time_bins, _tick_step(meta.time_bins, 50)):
        x = t * 2 - 1
        out.append(((x, -1, -1), (x, -1, 1), GRID_FAINT))
        out.append(((x, -1, 1), (x, 1, 1), GRID_FAINT))
    for f in _ticks(meta.freq_bins, _tick_step(meta.freq_bins, 20)):
        z = f * 2 - 1
        out.append(((-1, -1, z), (1, -1, z), GRID_FAINT))
        out.append(((-1, -1, z), (-1, 1, z), GRID_FAINT))
    for index in range(len(EXPECTED)):
        y = plane_y(index / 3)
        out.append(((-1, y, 1), (1, y, 1), GRID_FAINT))
        out.append(((-1, y, -1), (-1, y, 1), GRID_FAINT))
    return out


def playhead_segments(x: float) -> list[tuple]:
    """``playheadLines``: the slice frame plus 11 translucent hatch lines."""
    out = [
        ((x, -1, -1), (x, -1, 1), HEAD),
        ((x, 1, -1), (x, 1, 1), HEAD),
        ((x, -1, -1), (x, 1, -1), HEAD),
        ((x, -1, 1), (x, 1, 1), HEAD),
    ]
    for i in range(1, 12):
        z = -1 + (i / 12) * 2
        out.append(((x, -1, z), (x, 1, z), HEAD_PLANE))
    return out


_SPRITES: dict[int, np.ndarray] = {}


def _disc(diameter: float, cx: float, cy: float) -> tuple[np.ndarray, int, int]:
    """Coverage of a ``gl_PointSize`` disc centred at (cx, cy), 4x supersampled."""
    r = diameter / 2
    x0, y0 = int(math.floor(cx - r)), int(math.floor(cy - r))
    n = int(math.ceil(cx + r)) - x0 + 1
    m = int(math.ceil(cy + r)) - y0 + 1
    ss = 4
    xs = x0 + (np.arange(n * ss) + 0.5) / ss
    ys = y0 + (np.arange(m * ss) + 0.5) / ss
    inside = ((xs[None, :] - cx) ** 2 + (ys[:, None] - cy) ** 2) <= r * r
    cover = inside.reshape(m, ss, n, ss).mean(axis=(1, 3)).astype(np.float32)
    return cover, y0, x0


class CubeFrames:
    """Static layers built once; ``frame(t)`` composes one RGB frame."""

    def __init__(self, doc: dict, name: str, width: int = WIDTH, height: int = HEIGHT, dpr: float = DPR):
        self.points, self.meta = parse_cube(doc, name)
        self.width, self.height, self.dpr = width, height, dpr
        self.project = Projector(width, height)
        self.line_px = max(1, round(dpr))
        css_h = height / dpr
        self.u_point = max(1.6, min(5.0, css_h / 150)) * dpr * 0.85
        self.band = 1.01 * (2 / max(1, self.meta.time_bins - 1))
        self.rem = 16 * dpr
        # Background + grid (GL canvas, drawn under the points).
        self.bg = np.empty((height, width, 3), dtype=np.float32)
        self.bg[:] = np.asarray(CLEAR, dtype=np.float32)
        _draw_segments(self.bg, grid_segments(self.meta), self.project, self.line_px)
        # Every point in its normal (not-near) state, premultiplied RGBA, in layer order.
        self.points_rgba = np.zeros((height, width, 4), dtype=np.float32)
        self.screen = []
        for layer in EXPECTED:
            color = np.asarray(LAYER_COLORS[layer], dtype=np.float32)
            for p in self.points:
                if p["layer"] != layer:
                    continue
                x = float(p["t"]) * 2 - 1
                px, py = self.project(x, plane_y(float(p["z"])), float(p["f"]) * 2 - 1)
                g = min(1.0, max(0.0, float(p["v"])))
                self.screen.append((x, px, py, g, layer))
                self._splat(self.points_rgba, color, px, py, g, near=False)
        # Label canvas (ticks, axis names, plane names), drawn over the GL canvas.
        self.labels = self._label_layer()
        self.static = self._compose_static()

    # -- drawing helpers --
    def _style(self, g: float, near: bool, color: np.ndarray) -> tuple[float, np.ndarray, float]:
        alpha = (0.35 + 0.65 * g) * (1.0 if near else 0.72)
        rgb = color + (1.0 - color) * (0.45 if near else 0.0)
        size = self.u_point * (0.6 + 0.8 * g) * (1.0 + (0.9 if near else 0.0))
        return alpha, rgb, size

    def _splat(self, dst: np.ndarray, color: np.ndarray, px: float, py: float, g: float, near: bool) -> None:
        alpha, rgb, size = self._style(g, near, color)
        cover, y0, x0 = _disc(size, px, py)
        h, w = cover.shape
        ya, xa = max(0, y0), max(0, x0)
        yb, xb = min(dst.shape[0], y0 + h), min(dst.shape[1], x0 + w)
        if yb <= ya or xb <= xa:
            return
        a = (cover[ya - y0 : yb - y0, xa - x0 : xb - x0] * alpha)[..., None]
        region = dst[ya:yb, xa:xb]
        if dst.shape[2] == 4:
            region *= 1.0 - a
            region[..., :3] += a * rgb
            region[..., 3:] += a
        else:
            region *= 1.0 - a
            region += a * rgb

    def _text_layer(self) -> tuple[Image.Image, ImageDraw.ImageDraw]:
        image = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 0))
        return image, ImageDraw.Draw(image)

    def _label_font(self) -> ImageFont.ImageFont:
        size = round(max(10, min(15, self.height / self.dpr / 48)) * self.dpr + 1e-9)
        return _font(int(size))

    def _label_layer(self) -> np.ndarray:
        image, draw = self._text_layer()
        font = self._label_font()
        ink = INK[:3] + (round(INK[3] * 255),)

        def text(value, point, dx=0, dy=0, align="center", fill=ink):
            anchor = {"center": "mm", "left": "lm"}[align]
            draw.text((point[0] + dx * self.dpr, point[1] + dy * self.dpr), value, font=font, fill=fill, anchor=anchor)

        p = self.project
        meta = self.meta
        t_step = _tick_step(meta.time_bins, 50)
        for b in range(0, meta.time_bins, t_step):
            text(str(b), p((b / (meta.time_bins - 1)) * 2 - 1, -1, -1), 0, 14)
        text("time bin", p(0, -1, -1.28), 0, 26)
        f_step = _tick_step(meta.freq_bins, 20)
        for b in range(0, meta.freq_bins, f_step):
            text(str(b), p(1, -1, (b / (meta.freq_bins - 1)) * 2 - 1), 14, 8, "left")
        text("freq bin", p(1.3, -1, 0), 20, 18, "left")
        for index, layer in enumerate(EXPECTED):
            c = LAYER_COLORS[layer]
            text(layer, p(1, plane_y(index / 3), 1), 10, 0, "left", tuple(round(v * 255) for v in c) + (255,))
        text("layer (+value)", p(-1, 1, 1), 0, -14, "left")
        self._overlays(draw)
        return _premultiplied(image)

    def _pill(self, draw, xy, text, font, color, anchor_right=False, fill=(8, 12, 20, 158)):
        pad_x, pad_y = 0.5 * self.rem, 0.22 * self.rem
        box = draw.textbbox((0, 0), text, font=font, anchor="lt")
        w, h = box[2] - box[0] + 2 * pad_x, box[3] - box[1] + 2 * pad_y
        x, y = xy
        if anchor_right:
            x -= w
        draw.rounded_rectangle((x, y - h, x + w, y), radius=8 * self.dpr / 1.5, fill=fill, outline=(138, 243, 226, 56), width=1)
        draw.text((x + pad_x, y - h + pad_y), text, font=font, fill=color, anchor="lt")

    def _overlays(self, draw) -> None:
        """Static DOM overlays of the Cube tab: legend, title, badge, caption."""
        rem = self.rem
        meta = self.meta
        small = _font(round(0.72 * rem))
        # Legend (top-left).
        x, y = 0.5 * rem, 0.5 * rem
        rows = len(EXPECTED)
        row_h = 0.72 * rem * 1.35
        draw.rounded_rectangle((x, y, x + 8 * rem, y + rows * row_h + 0.6 * rem), radius=8, fill=(8, 12, 20, 158), outline=(138, 243, 226, 56))
        for i, layer in enumerate(EXPECTED):
            cy = y + 0.3 * rem + row_h * (i + 0.5)
            c = tuple(round(v * 255) for v in LAYER_COLORS[layer])
            r = 0.275 * rem
            draw.ellipse((x + 0.55 * rem, cy - r, x + 0.55 * rem + 2 * r, cy + r), fill=c)
            draw.text((x + 0.55 * rem + 2 * r + 0.4 * rem, cy), layer, font=small, fill=(220, 231, 245, 255), anchor="lm")
        # Title (top centre).
        title_font = _font(round(0.85 * rem), bold=True)
        box = draw.textbbox((0, 0), meta.title, font=title_font, anchor="lt")
        w = box[2] - box[0] + 1.2 * rem
        h = box[3] - box[1] + 0.4 * rem + 0.3 * rem
        tx = (self.width - w) / 2
        draw.rounded_rectangle((tx, 0.5 * rem, tx + w, 0.5 * rem + h), radius=8, fill=(8, 12, 20, 158), outline=(138, 243, 226, 56))
        draw.text((self.width / 2, 0.5 * rem + h / 2), meta.title, font=title_font, fill=(220, 231, 245, 255), anchor="mm")
        # Coverage badge (under the title).
        badge = f"cube covers 0:00\u2013{format_clock(meta.covers_s)} of {format_clock(meta.duration_s)}"
        if meta.revision:
            badge += f" \u00b7 rev {meta.revision}"
        full = meta.covers_s >= meta.duration_s - 1
        bfont = _font(round(0.68 * rem))
        bb = draw.textbbox((0, 0), badge, font=bfont, anchor="lt")
        bw, bh = bb[2] - bb[0] + rem, bb[3] - bb[1] + 0.4 * rem
        bx = (self.width - bw) / 2
        draw.rounded_rectangle((bx, 2.35 * rem, bx + bw, 2.35 * rem + bh), radius=8, fill=(8, 12, 20, 158), outline=(138, 243, 226, 56))
        draw.text((self.width / 2, 2.35 * rem + bh / 2), badge, font=bfont, fill=(138, 243, 226, 255) if full else (240, 180, 41, 255), anchor="mm")
        # Caption (bottom-left): the string loadCube returns.
        inv = "?" if meta.inv_hdr is None else f"{meta.inv_hdr:.4f}"
        caption = (
            f"{meta.title} \u00b7 inv_hdr {inv} \u00b7 {meta.points} points \u00b7 {format_clock(meta.duration_s)}"
            f" \u00b7 {meta.time_bins} time bins \u00b7 axes time bin / freq bin / layer"
        )
        self._pill(draw, (0.5 * rem, self.height - 2.1 * rem), caption, _font(round(0.7 * rem)), (185, 200, 219, 255))

    def _compose_static(self) -> np.ndarray:
        rgb = self.points_rgba[..., :3] + self.bg * (1.0 - self.points_rgba[..., 3:])
        return self.labels[..., :3] + rgb * (1.0 - self.labels[..., 3:])

    def plane_bbox(self, x: float) -> tuple[int, int, int, int]:
        pts = [self.project(x, y, z) for y in (-1, 1) for z in (-1, 1)]
        pad = int(self.u_point * 2.6) + self.line_px + 4
        x0 = max(0, int(min(p[0] for p in pts)) - pad)
        x1 = min(self.width, int(max(p[0] for p in pts)) + pad)
        y0 = max(0, int(min(p[1] for p in pts)) - pad)
        y1 = min(self.height, int(max(p[1] for p in pts)) + pad)
        return y0, y1, x0, x1

    def frame(self, seconds: float) -> tuple[np.ndarray, Playhead]:
        head = playhead(seconds, self.meta)
        x = head.x * 2 - 1
        out = self.static.copy()
        y0, y1, x0, x1 = self.plane_bbox(x)
        region = self.bg[y0:y1, x0:x1].copy()
        _draw_segments_region(region, playhead_segments(x), self.project, self.line_px, y0, x0)
        pts = self.points_rgba[y0:y1, x0:x1]
        region = pts[..., :3] + region * (1.0 - pts[..., 3:])
        for px_, px, py, g, layer in self.screen:
            if abs(px_ - x) < self.band:
                self._splat_region(region, np.asarray(LAYER_COLORS[layer], dtype=np.float32), px - x0, py - y0, g)
        lab = self.labels[y0:y1, x0:x1]
        out[y0:y1, x0:x1] = lab[..., :3] + region * (1.0 - lab[..., 3:])
        frame = np.clip(out * 255.0 + 0.5, 0, 255).astype(np.uint8)
        self._dynamic_overlays(frame, head)
        return frame, head

    def _splat_region(self, region, color, px, py, g):
        self._splat(region, color, px, py, g, near=True)

    def _dynamic_overlays(self, frame: np.ndarray, head: Playhead) -> None:
        """Bin label on the label canvas, then the clock readout and scrub bar (drawn on crops)."""
        rem, meta = self.rem, self.meta
        font = self._label_font()
        bx, by = self.project(head.x * 2 - 1, 1, -1)
        label = f"bin {head.bin}"
        lx, ly = bx, by - 12 * self.dpr
        size = font.getbbox(label, anchor="mm")
        pad = 4
        crop = (
            max(0, int(lx + size[0]) - pad), max(0, int(ly + size[1]) - pad),
            min(self.width, int(lx + size[2]) + pad), min(self.height, int(ly + size[3]) + pad),
        )
        if crop[2] > crop[0] and crop[3] > crop[1]:
            self._on_crop(frame, crop, lambda d, ox, oy: d.text(
                (lx - ox, ly - oy), label, font=font, fill=BIN_INK[:3] + (round(BIN_INK[3] * 255),), anchor="mm"))
        clock = f"{format_clock(head.seconds)} / {format_clock(meta.duration_s)} \u00b7 bin {head.bin} / {meta.time_bins - 1}"
        if head.beyond:
            clock += " \u00b7 beyond cube"
        fraction = min(1.0, max(0.0, head.seconds / meta.duration_s))
        top = int(self.height - 4.0 * rem)

        def bottom(d, ox, oy):
            self._pill(d, (self.width - 0.5 * rem - ox, self.height - 2.1 * rem - oy), clock,
                       _font(round(0.72 * rem)), (41, 211, 255, 255), anchor_right=True)
            # Scrub (range input, accent #29d3ff): fraction of the clip's duration.
            left, right = 0.9 * rem, self.width - 0.9 * rem
            cy = self.height - 1.05 * rem - oy
            track = 0.22 * rem
            d.rounded_rectangle((left, cy - track, right, cy + track), radius=track, fill=(90, 96, 110, 255))
            knob = left + (right - left) * fraction
            d.rounded_rectangle((left, cy - track, knob, cy + track), radius=track, fill=(41, 211, 255, 255))
            r = 0.42 * rem
            d.ellipse((knob - r, cy - r, knob + r, cy + r), fill=(41, 211, 255, 255))

        self._on_crop(frame, (0, top, self.width, self.height), bottom)

    def _on_crop(self, frame: np.ndarray, box: tuple[int, int, int, int], paint) -> None:
        x0, y0, x1, y1 = box
        base = Image.fromarray(frame[y0:y1, x0:x1]).convert("RGBA")
        layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
        paint(ImageDraw.Draw(layer), x0, y0)
        base.alpha_composite(layer)
        frame[y0:y1, x0:x1] = np.asarray(base.convert("RGB"))


def _draw_segments_region(region, segs, project, width_px, y0, x0):
    class Shifted:
        def __call__(self, x, y, z):
            px, py = project(x, y, z)
            return px - x0, py - y0

    _draw_segments(region, segs, Shifted(), width_px)


def _premultiplied(image: Image.Image) -> np.ndarray:
    arr = np.asarray(image, dtype=np.float32) / 255.0
    arr[..., :3] *= arr[..., 3:]
    return arr


def render_clip_mp4(
    wav: Path,
    cube_json: Path,
    output: Path,
    *,
    name: str | None = None,
    fps: int = FPS,
    width: int = WIDTH,
    height: int = HEIGHT,
) -> Path:
    """Render the bitcube animation over the clip audio and write ``<output>.slices.json``."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise VideoError("ffmpeg is not on PATH")
    for path in (wav, cube_json):
        if not Path(path).is_file():
            raise VideoError(f"video input is missing: {Path(path).name}")
    doc = json.loads(Path(cube_json).read_text(encoding="utf-8"))
    frames = CubeFrames(doc, name or Path(cube_json).parent.name, width, height)
    meta = frames.meta
    count = frame_count(meta.duration_s, fps)
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg, "-y", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}", "-framerate", str(fps), "-i", "-",
        "-i", str(wav),
        "-map", "0:v", "-map", "1:a",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
        "-fps_mode", "cfr", "-r", str(fps), "-g", str(fps),
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(output),
    ]
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    bins = []
    try:
        assert process.stdin is not None
        for i in range(count):
            image, head = frames.frame(i / fps)
            bins.append(head.bin)
            process.stdin.write(image.tobytes())
        process.stdin.close()
    except BrokenPipeError:
        pass
    stderr = process.stderr.read().decode("utf-8", "replace") if process.stderr else ""
    code = process.wait()
    if code != 0 or not output.is_file() or output.stat().st_size < 1024:
        raise VideoError(f"ffmpeg failed to write {output.name}: {stderr[-500:]}")
    sidecar = output.with_suffix(".slices.json")
    sidecar.write_text(
        json.dumps(
            {
                "schema": "gen-audio/cube-slices/v1",
                "video": output.name,
                "cube": Path(cube_json).name,
                "fps": fps,
                "frames": count,
                "width": width,
                "height": height,
                "duration_s": meta.duration_s,
                "bin_seconds": meta.bin_seconds,
                "time_bins": meta.time_bins,
                "covers_s": meta.covers_s,
                "rule": "frame i shows t = i / fps; bin = clamp(floor(t / bin_seconds), 0, time_bins - 1)",
                "bins": bins,
            },
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    return output
