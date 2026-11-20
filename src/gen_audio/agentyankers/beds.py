"""Procedural laugh, stinger, and bed cues for an AgentYankers episode.

These are show beds mixed by this package. They are not recordings and they
are not a substitute for a VibeVoice voiceover.
"""

from __future__ import annotations

import numpy as np

from gen_audio.agentyankers.contract import SAMPLE_RATE


def synthesize_laugh(sample_rate: int, duration: float, seed: int) -> np.ndarray:
    """Short voiced bursts, peaked well under full scale."""
    _rate(sample_rate)
    count = _count(sample_rate, duration)
    rng = np.random.default_rng(int(seed) & 0xFFFFFFFF)
    audio = np.zeros(count, dtype=np.float64)
    bursts = 4
    for index in range(bursts):
        center = int((index + 0.55) * count / bursts)
        width = max(8, int(0.07 * sample_rate))
        start = max(0, center - width // 2)
        end = min(count, start + width)
        length = end - start
        if length <= 0:
            continue
        time = np.arange(length) / sample_rate
        envelope = np.sin(np.linspace(0.0, np.pi, length)) ** 2
        frequency = 170.0 + (35.0 * index)
        audio[start:end] += 0.32 * envelope * np.sin(2 * np.pi * frequency * time)
        audio[start:end] += 0.04 * envelope * rng.standard_normal(length)
    return _peak(audio, 0.35)


def synthesize_stinger(
    sample_rate: int,
    duration: float,
    seed: int,
    *,
    outro: bool = False,
) -> np.ndarray:
    """Two-note sting. The outro drops a fifth instead of rising."""
    _rate(sample_rate)
    count = _count(sample_rate, duration)
    time = np.arange(count) / sample_rate
    root = 220.0 if outro else 392.0
    fifth = root * (2.0 / 3.0 if outro else 3.0 / 2.0)
    blend = np.clip(time / max(duration, 1e-3), 0.0, 1.0)
    tone = (1.0 - blend) * np.sin(2 * np.pi * root * time) + blend * np.sin(2 * np.pi * fifth * time)
    decay = np.exp(-3.2 * time / max(duration, 1e-3))
    wobble = 1.0 + (0.01 * np.sin(2 * np.pi * (2.0 + (int(seed) % 3)) * time))
    return _peak(tone * decay * wobble, 0.4)


def synthesize_bed(sample_rate: int, duration: float, seed: int) -> np.ndarray:
    """Quiet one-pole noise loop. The mixer applies the cue gain."""
    _rate(sample_rate)
    count = _count(sample_rate, duration)
    rng = np.random.default_rng((int(seed) + 17) & 0xFFFFFFFF)
    noise = rng.standard_normal(count)
    smoothed = np.empty(count, dtype=np.float64)
    follow = 0.0
    coefficient = 0.985
    for index, sample in enumerate(noise):
        follow = (coefficient * follow) + ((1.0 - coefficient) * float(sample))
        smoothed[index] = follow
    return _peak(smoothed, 0.2)


def apply_gain_db(audio: np.ndarray, gain_db: float) -> np.ndarray:
    scale = float(10.0 ** (float(gain_db) / 20.0))
    return np.asarray(audio, dtype=np.float32) * np.float32(scale)


def _rate(sample_rate: int) -> None:
    if int(sample_rate) != SAMPLE_RATE:
        raise ValueError("AgentYankers cues use 24000 Hz")


def _count(sample_rate: int, duration: float) -> int:
    count = int(round(float(duration) * int(sample_rate)))
    if count < 1:
        raise ValueError("cue duration is empty")
    return count


def _peak(audio: np.ndarray, limit: float) -> np.ndarray:
    values = np.asarray(audio, dtype=np.float64)
    peak = float(np.max(np.abs(values))) if values.size else 0.0
    if peak > 1e-8:
        values = values * (float(limit) / peak)
    return np.ascontiguousarray(values, dtype=np.float32)
