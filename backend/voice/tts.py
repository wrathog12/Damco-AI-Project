"""
TTS wrapper — Cartesia Sonic for text-to-speech.
Returns audio as numpy array (PCM int16).
"""
import numpy as np
from cartesia import Cartesia
from config import settings

_client: Cartesia | None = None


def _get_client() -> Cartesia:
    global _client
    if _client is None:
        _client = Cartesia(api_key=settings.cartesia_api_key)
    return _client


def synthesize(text: str, sample_rate: int = 24000) -> np.ndarray:
    """
    Convert text to speech using Cartesia Sonic.
    
    Args:
        text: text to synthesize (any language Cartesia supports)
        sample_rate: output sample rate (default 24000)
    
    Returns:
        numpy array of audio samples (int16)
    """
    if not text.strip():
        return np.array([], dtype=np.int16)

    client = _get_client()

    # Generate audio
    output = client.tts.bytes(
        model_id=settings.tts_model,
        transcript=text,
        voice={"mode": "id", "id": settings.tts_voice_id},
        language="hi",
        output_format={
            "container": "raw",
            "encoding": "pcm_s16le",
            "sample_rate": sample_rate,
        },
    )

    # tts.bytes() returns a generator of chunks in SDK v2.x
    audio_bytes = b"".join(output)
    audio = np.frombuffer(audio_bytes, dtype=np.int16)
    return audio


def synthesize_streaming(text: str, sample_rate: int = 24000):
    """
    Generator that yields audio chunks as they're generated.
    Use this for lower time-to-first-audio.
    
    Yields:
        numpy arrays of audio samples (int16)
    """
    if not text.strip():
        return

    client = _get_client()

    # Stream audio chunks
    for chunk in client.tts.sse(
        model_id=settings.tts_model,
        transcript=text,
        voice={"mode": "id", "id": settings.tts_voice_id},
        language="hi",
        output_format={
            "container": "raw",
            "encoding": "pcm_s16le",
            "sample_rate": sample_rate,
        },
    ):
        if hasattr(chunk, "audio") and chunk.audio:
            yield np.frombuffer(chunk.audio, dtype=np.int16)
