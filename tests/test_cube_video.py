"""The clip video is the Cube viewport's inverse-HDR bitcube, in sync with the audio.

The checks read the projection, camera and colours straight out of
apps/desktop/src/cubeview.ts and look for the slice plane in decoded frames, so
they hold the MP4 to the viewport rather than to the renderer's own copy.
"""

from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from gen_audio.asr_wer import AsrError
from gen_audio.assets import asset_object
from gen_audio.audio_io import write_wav
from gen_audio.pipeline import run_pipeline

ROOT = Path(__file__).resolve().parents[1]
CUBEVIEW = ROOT / "apps" / "desktop" / "src" / "cubeview.ts"
FPS = 25

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="ffmpeg is not on PATH")


def _viewport() -> dict:
    """Constants of the WebGL viewport, parsed from cubeview.ts."""
    src = CUBEVIEW.read_text(encoding="utf-8")

    def num(pattern: str) -> float:
        match = re.search(pattern, src)
        assert match, f"cubeview.ts no longer has {pattern}"
        return float(match.group(1))

    box = re.search(r"const BOX: \[number, number, number\] = \[([^\]]+)\]", src)
    colors = dict(re.findall(r"^\s+(signal|tonality|confidence|quality): \[([^\]]+)\],", src, re.M))
    head = re.search(r"const head = \[([^\]]+)\];", src)
    assert box and len(colors) == 4 and head
    return {
        "box": [float(v) for v in box.group(1).split(",")],
        "camera": num(r"const CAMERA = ([\d.]+);"),
        "scale": num(r"const SCALE = ([\d.]+);"),
        "lift": num(r"const LIFT = ([\d.]+);"),
        "yaw": num(r"yaw: (-?[\d.]+),"),
        "pitch": num(r"pitch: (-?[\d.]+),"),
        "colors": {k: [float(v) for v in vals.split(",")] for k, vals in colors.items()},
        "head": [float(v) for v in head.group(1).split(",")],
        "clear": [float(v) for v in re.search(r"gl\.clearColor\(([^)]+)\)", src).group(1).split(",")][:3],
    }


def _project(vp: dict, width: int, height: int, x: float, y: float, z: float) -> tuple[float, float]:
    """``project`` from cubeview.ts at zoom 1."""
    cy, sy = math.cos(vp["yaw"]), math.sin(vp["yaw"])
    cx, sx = math.cos(vp["pitch"]), math.sin(vp["pitch"])
    x, y, z = x * vp["box"][0], y * vp["box"][1], z * vp["box"][2]
    x1 = x * cy - z * sy
    z1 = x * sy + z * cy
    y2 = y * cx - z1 * sx
    z2 = y * sx + z1 * cx
    w = max(0.2, vp["camera"] + z2)
    nx, ny = x1 * vp["scale"] / w, y2 * vp["scale"] / w
    aspect = width / height
    if aspect > 1:
        nx /= aspect
    else:
        ny *= aspect
    ny += vp["lift"]
    return (nx + 1) / 2 * width, (1 - ny) / 2 * height


def _probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    return json.loads(out)


def _frame(path: Path, index: int) -> np.ndarray:
    """Decode exactly frame ``index`` (by frame number, not by seek time) at the stream's size."""
    video = next(s for s in _probe(path)["streams"] if s["codec_type"] == "video")
    width, height = int(video["width"]), int(video["height"])
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-vf", f"select=eq(n\\,{index})", "-frames:v", "1",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True, check=True,
    ).stdout
    assert len(raw) == width * height * 3, f"frame {index} did not decode"
    return np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 3)


def _slice_bin(frame: np.ndarray, vp: dict, time_bins: int) -> tuple[int, float]:
    """Time bin whose projected slice-plane frame is most cyan in the picture.

    Samples sit exactly on each candidate's four projected edges; neighbouring bins are
    a few pixels apart, so only the drawn bin lines up along all of them.
    """
    height, width = frame.shape[:2]
    rgb = frame.astype(np.float32)
    cyan = np.clip((rgb[..., 2] - rgb[..., 0]) / 160.0, 0, 1) * (rgb[..., 1] > 120)
    scores = []
    for b in range(time_bins):
        x = b / (time_bins - 1) * 2 - 1
        values = []
        for a, c in (((x, -1, -1), (x, -1, 1)), ((x, 1, -1), (x, 1, 1)), ((x, -1, -1), (x, 1, -1)), ((x, -1, 1), (x, 1, 1))):
            pa, pc = _project(vp, width, height, *a), _project(vp, width, height, *c)
            for s in np.linspace(0.08, 0.92, 32):
                # Pixel i covers [i, i + 1): the projected point lands in pixel floor(x).
                px = int(math.floor(pa[0] + (pc[0] - pa[0]) * s))
                py = int(math.floor(pa[1] + (pc[1] - pa[1]) * s))
                if 0 <= px < width and 0 <= py < height:
                    values.append(cyan[py, px])
        scores.append(float(np.mean(values)) if values else 0.0)
    best = int(np.argmax(scores))
    # Contrast: the drawn slice against the median candidate (grid lines and points score low).
    return best, scores[best] - float(np.median(scores))


def _expected_bin(seconds: float, bin_seconds: float, time_bins: int) -> int:
    """``getCubePlayhead``'s rule."""
    return max(0, min(time_bins - 1, math.floor(seconds / bin_seconds)))


def _longest_flat_run(values: np.ndarray) -> int:
    """Longest run of consecutive rows (or columns) with no visible content."""
    flat = (values.max(axis=1).max(axis=-1) - values.min(axis=1).min(axis=-1)) < 6
    best = run = 0
    for f in flat:
        run = run + 1 if f else 0
        best = max(best, run)
    return best


@pytest.fixture(scope="module")
def clip(tmp_path_factory, monkeypatch_module):
    out = tmp_path_factory.mktemp("cube_video")
    rate = 24000
    seconds = 3.0
    t = np.arange(int(rate * seconds)) / rate
    # A rising chirp with an amplitude envelope: points spread across bins and layers.
    audio = (0.35 * (0.6 + 0.4 * np.sin(2 * np.pi * 1.5 * t)) * np.sin(2 * np.pi * (150 + 400 * t) * t)).astype(np.float32)
    raw = out / "raw.wav"
    write_wav(raw, audio, rate, subtype="FLOAT")
    script = out / "script.txt"
    script.write_text("Speaker 1: hello\n", encoding="utf-8")

    def no_asr(*_args, **_kwargs):
        raise AsrError("no ASR in this test")

    monkeypatch_module.setattr("gen_audio.pipeline.transcribe", no_asr)
    manifest = run_pipeline(
        raw, out_dir=out, engine="tone", reference_text="hello", duration_target_s=seconds,
        script_asset=asset_object(script, kind="script", derived_from=[]),
    )
    assert manifest["videoError"] == "", manifest["videoError"]
    cube = json.loads((out / "cube.json").read_text(encoding="utf-8"))
    return out / "clip.mp4", cube, manifest


@pytest.fixture(scope="module")
def monkeypatch_module():
    patcher = pytest.MonkeyPatch()
    yield patcher
    patcher.undo()


def test_video_is_1080p_cfr_and_starts_with_the_audio(clip):
    path, cube, _ = clip
    info = _probe(path)
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    audio = next(s for s in info["streams"] if s["codec_type"] == "audio")
    assert (video["width"], video["height"]) == (1920, 1080)
    assert video["r_frame_rate"] == f"{FPS}/1"
    assert float(video.get("start_time", 0)) == pytest.approx(0, abs=1e-3)
    assert float(audio.get("start_time", 0)) == pytest.approx(0, abs=1 / FPS)
    assert int(video["nb_frames"]) == math.ceil(cube["duration_s"] * FPS - 1e-9)


def test_slice_plane_tracks_the_audio_clock_within_one_frame(clip):
    path, cube, _ = clip
    vp = _viewport()
    bins = cube["cube_shape_f_t"][1]
    count = math.ceil(cube["duration_s"] * FPS - 1e-9)
    for index in sorted({0, 5, 12, 25, 37, 50, 63, count - 1}):
        frame = _frame(path, index)
        found, score = _slice_bin(frame, vp, bins)
        t = index / FPS
        expected = _expected_bin(t, cube["bin_seconds"], bins)
        window = {_expected_bin(max(0.0, t + d / FPS), cube["bin_seconds"], bins) for d in (-1, 0, 1)}
        assert score > 0.35, f"frame {index}: no slice plane found (contrast {score:.2f})"
        assert found == expected, f"frame {index} t={t:.2f}s shows bin {found}, audio is at bin {expected}"
        assert found in window


def test_frames_have_no_static_png_panel_and_no_empty_bands(clip):
    path, _, _ = clip
    vp = _viewport()
    frame = _frame(path, 30)
    white = (frame > 235).all(axis=-1).mean()
    assert white < 0.002, f"{white:.1%} of the frame is white: a pasted matplotlib panel"
    clear = np.round(np.asarray(vp["clear"]) * 255)
    background = (np.abs(frame.astype(np.int16) - clear).max(axis=-1) <= 6).mean()
    assert background > 0.6, "the frame background is not the viewport's dark clear colour"
    assert _longest_flat_run(frame) < 0.05 * frame.shape[0], "an empty horizontal band crosses the frame"
    assert _longest_flat_run(frame.transpose(1, 0, 2)) < 0.05 * frame.shape[1], "an empty vertical band crosses the frame"


def test_layer_points_use_the_viewport_colours(clip):
    path, _, _ = clip
    vp = _viewport()
    frame = _frame(path, 40).astype(np.int16)
    for layer, color in vp["colors"].items():
        target = np.round(np.asarray(color) * 255)
        # Points blend over the dark clear colour, so look for the hue, not the exact value.
        near = (np.abs(frame - target).max(axis=-1) < 70).sum()
        assert near > 30, f"no {layer} points in the frame"


def test_slices_sidecar_records_the_rule_for_every_frame(clip):
    path, cube, manifest = clip
    sidecar = json.loads(path.with_suffix(".slices.json").read_text(encoding="utf-8"))
    assert sidecar["fps"] == FPS
    assert sidecar["bins"] == [
        _expected_bin(i / FPS, cube["bin_seconds"], cube["cube_shape_f_t"][1]) for i in range(sidecar["frames"])
    ]
    assert manifest["files"]["videoSlices"] == path.with_suffix(".slices.json").name
    assert manifest["assets"]["video"]["derived_from"] == [manifest["assets"]["wav"]["uid"], manifest["assets"]["cube"]["uid"]]


def test_renderer_constants_match_cubeview_ts():
    from gen_audio import video_render

    vp = _viewport()
    assert list(video_render.BOX) == vp["box"]
    assert (video_render.CAMERA, video_render.SCALE, video_render.LIFT) == (vp["camera"], vp["scale"], vp["lift"])
    assert (video_render.YAW, video_render.PITCH) == (vp["yaw"], vp["pitch"])
    assert {k: list(v) for k, v in video_render.LAYER_COLORS.items()} == vp["colors"]
    assert list(video_render.HEAD) == vp["head"]
    assert list(video_render.CLEAR) == vp["clear"]
