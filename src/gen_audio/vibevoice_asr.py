"""Local VibeVoice batch and checkpoint-driven streaming speech recognition."""

from __future__ import annotations

import json
from math import gcd
from pathlib import Path
from time import perf_counter


class VibeVoiceASR:
    def __init__(self, directory: Path | str, *, streaming: bool = False, device: str = "cpu"):
        import torch

        self.directory = Path(directory)
        if not self.directory.is_dir():
            raise FileNotFoundError(f"VibeVoice checkpoint directory unavailable: {self.directory}")
        self.streaming = streaming
        self.device = device
        started = perf_counter()
        if streaming:
            from vibevoice.modular.modeling_vibevoice_asr import VibeVoiceASRForConditionalGeneration
            from vibevoice.processor.vibevoice_asr_processor import VibeVoiceASRProcessor

            config = json.loads((self.directory / "preprocessor_config.json").read_text(encoding="utf-8"))
            self.rate = config["target_sample_rate"]
            frame_seconds = config["speech_tok_compress_ratio"] / self.rate
            self.chunk_duration = config["chunk_frames"] * frame_seconds
            self.lookahead_duration = config["lookahead_frames"] * frame_seconds
            self.processor = VibeVoiceASRProcessor.from_pretrained(str(self.directory))
            if self.processor.tokenizer.text_chunk_end_id is None:
                raise ValueError("The VibeVoice tokenizer lacks the streaming chunk-end token")
            dtype = torch.bfloat16 if device == "cuda" else torch.float32
            self.model = VibeVoiceASRForConditionalGeneration.from_pretrained(
                self.directory, dtype=dtype, attn_implementation="sdpa", local_files_only=True,
            ).to(device).eval()
        else:
            from transformers import AutoProcessor, VibeVoiceAsrForConditionalGeneration

            self.processor = AutoProcessor.from_pretrained(self.directory, local_files_only=True)
            self.rate = self.processor.feature_extractor.sampling_rate
            self.model = VibeVoiceAsrForConditionalGeneration.from_pretrained(
                self.directory, local_files_only=True, dtype=torch.bfloat16, device_map={"": device},
            ).eval()
        self.load_seconds = perf_counter() - started

    def prepare(self, audio, rate: int):
        from scipy.signal import resample_poly

        if not audio.size or rate <= 0:
            raise ValueError("Expected nonempty audio with a positive sample rate")
        if rate == self.rate:
            return audio
        factor = gcd(rate, self.rate)
        return resample_poly(audio, self.rate // factor, rate // factor)

    def stream(self, audio, rate: int, *, context: str | None = None, max_tokens: int = 256):
        import torch

        if not self.streaming:
            raise ValueError("This checkpoint supports whole-file recognition")
        audio = self.prepare(audio, rate)
        # Upstream's generate method applies its own inference context. Avoid
        # holding a thread-local torch context across a yield to the caller.
        for index, count, text in self.model.streaming_generate(
            audio_tensor=torch.from_numpy(audio), tokenizer=self.processor.tokenizer,
            chunk_duration=self.chunk_duration, text_audio_delay=self.lookahead_duration,
            sample_rate=self.rate, max_new_tokens_per_chunk=max_tokens,
            temperature=0.0, context_info=context,
        ):
            yield {"index": index + 1, "count": count, "text": text}

    def transcribe(self, audio, rate: int, *, context: str | None = None, max_tokens: int = 512) -> dict:
        import torch

        started = perf_counter()
        if self.streaming:
            chunks = list(self.stream(audio, rate, context=context, max_tokens=max_tokens))
            result = {"text": "".join(chunk["text"] for chunk in chunks).strip(), "chunks": chunks}
        else:
            inputs = self.processor.apply_transcription_request(audio=self.prepare(audio, rate), prompt=context)
            inputs = inputs.to(self.model.device, self.model.dtype)
            with torch.inference_mode():
                ids = self.model.generate(**inputs, max_new_tokens=max_tokens, do_sample=False)
            tokens = ids[:, inputs["input_ids"].shape[1]:]
            result = {
                "text": self.processor.decode(tokens, return_format="transcription_only")[0],
                "segments": self.processor.decode(tokens, return_format="parsed")[0],
                "token_limit_reached": tokens.shape[1] >= max_tokens,
            }
        return {
            **result, "model": self.directory.name, "device": self.device,
            "audioSeconds": audio.size / rate, "loadSeconds": round(self.load_seconds, 3),
            "inferenceSeconds": round(perf_counter() - started, 3),
        }
