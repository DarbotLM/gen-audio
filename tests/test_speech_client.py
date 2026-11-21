from email.message import Message
from io import BytesIO

import numpy as np
import pytest
import soundfile as sf

from gen_audio import speech_client
from gen_audio.adapters import synthesize
from gen_audio.cast import CastMap, Turn, VoiceAssignment


@pytest.mark.parametrize(
    ("media_type", "encoding", "dtype"),
    [
        ("audio/L16;rate=16000;channels=2", None, ">i2"),
        ("audio/L16", "pcm_s16le", "<i2"),
        ("application/octet-stream", "pcm_s16le", "<i2"),
    ],
)
def test_pcm_byte_order_and_downmix(media_type, encoding, dtype):
    headers = Message()
    headers["Content-Type"] = media_type
    if encoding:
        headers["X-Audio-Encoding"] = encoding
        headers["X-Audio-Sample-Rate"] = "16000"
        headers["X-Audio-Channels"] = "2"
    data = np.array([[4096, 12288], [-4096, -12288]], dtype=dtype).tobytes()
    mono, rate = speech_client.decode_audio(data, headers)
    assert rate == 16000
    np.testing.assert_allclose(mono, [0.25, -0.25])
    with pytest.raises(ValueError, match="incomplete"):
        speech_client.decode_audio(data[:-1], headers)


def test_flac_decodes_to_mono():
    headers = Message()
    headers["Content-Type"] = "audio/flac"
    sink = BytesIO()
    sf.write(sink, np.array([[0.25, 0.75], [-0.25, -0.75]]), 16000, format="FLAC")
    samples, sample_rate = speech_client.decode_audio(sink.getvalue(), headers)
    assert sample_rate == 16000
    np.testing.assert_allclose(samples, [0.5, -0.5], atol=1e-4)


def test_realtime_cast_reports_actual_resampled_offsets(tmp_path, monkeypatch):
    monkeypatch.setenv("GEN_AUDIO_DLM_SPEECH_URL", "http://127.0.0.1:8999")
    samples = {"en-Carter_man": (np.ones(1600) / 2, 16000),
               "en-Emma_woman": (np.ones(2205) / 2, 22050)}
    monkeypatch.setattr(speech_client, "check_vibevoice_provider", lambda: None)
    monkeypatch.setattr(speech_client, "vibevoice_realtime", lambda text, voice: samples[voice])
    cast = CastMap("adaptive", "en-us", 1.0, {
        "1": VoiceAssignment("1", "Carter", "en-Carter_man", "en-us", 1.0, "vibevoice_realtime"),
        "2": VoiceAssignment("2", "Emma", "en-Emma_woman", "en-us", 1.0, "livevoice"),
    })
    output = tmp_path / "render.wav"
    result = synthesize("adaptive", [Turn("1", "One.", None, 1), Turn("2", "Two.", None, 2)], cast, output)
    assert result["samples"] == 8400
    assert result["sample_rate"] == 24000
    assert result["speech"]["segments"] == [
        {"speaker_idx": 0, "start_sample": 0, "end_sample": 2400},
        {"speaker_idx": 1, "start_sample": 6000, "end_sample": 8400},
    ]
    assert [item["engine"] for item in result["speech"]["speakers"]] == ["vibevoice_realtime", "livevoice"]
