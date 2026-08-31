"""Local speech-to-text for videos that have no TikTok captions.

Backend preference: mlx-whisper (Apple Silicon; shells out to the ffmpeg CLI)
then faster-whisper (portable; decodes audio itself, no ffmpeg needed).
Models download from Hugging Face on first use (~500MB for `small`) into the
HF cache and are reused forever after.

Everything runs on-device: no API, no cost, no audio leaves the machine.
"""

from __future__ import annotations

import os
import shutil

os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")

MODELS = ("tiny", "base", "small", "medium", "large-v3")
DEFAULT_MODEL = "small"  # smallest model that handles non-English speech well

_state: dict = {"checked": False, "kind": None, "why": None}
_faster_models: dict = {}


def backend() -> str | None:
    """'mlx' | 'faster' | None, decided once per process."""
    if not _state["checked"]:
        _state["checked"] = True
        try:
            import mlx_whisper  # noqa: F401

            if shutil.which("ffmpeg"):
                _state["kind"] = "mlx"
            else:
                _state["why"] = "mlx-whisper installed but ffmpeg not on PATH"
        except ImportError:
            pass
        if _state["kind"] is None:
            try:
                import faster_whisper  # noqa: F401

                _state["kind"] = "faster"
            except ImportError:
                _state["why"] = _state["why"] or "no whisper backend installed"
    return _state["kind"]


def unavailable_reason() -> str:
    backend()
    return _state["why"] or "unknown"


def _mlx_repo(model: str) -> str:
    if "/" in model:  # full HF repo id passed through
        return model
    return f"mlx-community/whisper-{model}-mlx"


def transcribe(path, model: str = DEFAULT_MODEL) -> tuple[str | None, list[dict]]:
    """Transcribe an audio/video file. Returns (language_code, segments).

    Segments use the same {start, end, text} shape as caption cues. The
    language code is whisper-style ('fr', 'en'), distinct from TikTok's
    caption codes ('fra-FR'), which keeps the two kinds of track apart.
    """
    kind = backend()
    if kind is None:
        raise RuntimeError(f"no ASR backend: {unavailable_reason()}")

    if kind == "mlx":
        import mlx_whisper

        result = mlx_whisper.transcribe(str(path), path_or_hf_repo=_mlx_repo(model), verbose=None)
        lang = result.get("language")
        raw = [(float(s["start"]), float(s["end"]), str(s.get("text", "")),
                s.get("no_speech_prob"), s.get("avg_logprob"), s.get("compression_ratio"))
               for s in result.get("segments", [])]
    else:
        from faster_whisper import WhisperModel

        m = _faster_models.get(model)
        if m is None:
            m = _faster_models[model] = WhisperModel(model, compute_type="int8")
        segs, info = m.transcribe(str(path), vad_filter=True)
        lang = info.language
        raw = [(s.start, s.end, s.text, s.no_speech_prob, s.avg_logprob, s.compression_ratio)
               for s in segs]

    segments: list[dict] = []
    for start, end, text, nsp, alp, cr in raw:
        text = text.strip()
        if not text or not _looks_like_speech(nsp, alp, cr):
            continue
        if segments and segments[-1]["text"] == text:  # whisper stutter
            segments[-1]["end"] = round(end, 3)
        else:
            segments.append({"start": round(start, 3), "end": round(end, 3), "text": text})
    return lang, segments


def _looks_like_speech(no_speech_prob, avg_logprob, compression_ratio) -> bool:
    """Reject whisper hallucinations on music/ambient audio.

    Calibrated on TikTok audio: real speech (even over loud music) scores
    avg_logprob ≳ -0.9; hallucinated noise scores ≲ -3. compression_ratio
    > 2.4 (openai-whisper's own threshold) kills repetition loops ("a little
    bit of a little bit of ..."). The classic silence rule (no_speech > 0.6
    and logprob < -1) is kept as a final net. Accurately-heard song lyrics
    pass all three on purpose: they are real audio content — the caller can
    judge them by the track's whisper provenance.
    """
    if compression_ratio is not None and compression_ratio > 2.4:
        return False
    if avg_logprob is None:
        return True
    if avg_logprob < -2.0:
        return False
    if no_speech_prob is not None and no_speech_prob > 0.6 and avg_logprob < -1.0:
        return False
    return True
