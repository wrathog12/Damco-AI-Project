"""
STT wrapper — Deepgram Nova-3 for speech-to-text.

Uses the Deepgram Python SDK (same approach as Battery Smart project).
The SDK handles authentication, protocol details, and retries internally.

Uses language="multi" for multilingual support (Hindi, English, Bengali,
Marathi, Hinglish) without needing explicit language detection.

**Benchmark only.** The live pipeline uses Pipecat's streaming
`DeepgramSTTService` (see `voice/pipeline.py`); this batch-upload path is kept
because `test.py` is the repo's only per-stage timing tool and P5 compares
against the numbers it produced before the migration. Do not wire it back into
the voice loop — uploading a whole WAV after the caller stops talking is exactly
the latency P2 removed.
"""
import io
import wave
import numpy as np
from config import settings

# ── Deepgram SDK client (singleton) ───────────────────
_dg_client = None


def _get_client():
    global _dg_client
    if _dg_client is None:
        from deepgram import DeepgramClient
        _dg_client = DeepgramClient(settings.deepgram_api_key)
        print("[STT] Deepgram SDK client initialized")
    return _dg_client


def transcribe(audio_data: np.ndarray, sample_rate: int = 48000) -> tuple[str, str]:
    """
    Transcribe audio data using Deepgram Nova-3 via the official SDK.

    Args:
        audio_data: numpy array of audio samples (int16 or float32)
        sample_rate: sample rate in Hz (default 48000, standard for WebRTC)

    Returns:
        (transcript_text, detected_language)
    """
    # Handle 2D arrays from FastRTC (shape: 1, num_samples)
    if audio_data.ndim == 2:
        audio_data = audio_data.flatten()

    # Convert to int16 if needed
    if audio_data.dtype == np.float32:
        audio_data = (audio_data * 32767).astype(np.int16)
    elif audio_data.dtype != np.int16:
        audio_data = audio_data.astype(np.int16)

    # Convert to WAV bytes (SDK expects audio file bytes)
    pcm_bytes = audio_data.tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
    wav_bytes = buf.getvalue()

    # Quick validation: skip if audio is empty/silent
    if audio_data.size == 0:
        return "", "en"

    # Try SDK first, fallback to raw REST
    try:
        return _transcribe_sdk(wav_bytes)
    except Exception as e:
        print(f"[STT] SDK failed ({e}), falling back to raw REST")
        return _transcribe_rest(wav_bytes)


def _transcribe_sdk(wav_bytes: bytes) -> tuple[str, str]:
    """
    Transcribe using the Deepgram Python SDK (PrerecordedOptions).
    This is the same approach used in Battery Smart — proven to work.
    """
    from deepgram import PrerecordedOptions

    client = _get_client()

    options = PrerecordedOptions(
        model=settings.stt_model,
        language="multi",       # multilingual: Hindi, English, Bengali, Marathi, Hinglish
        smart_format=True,
        punctuate=True,
    )

    # Send audio to Deepgram via SDK
    response = client.listen.rest.v("1").transcribe_file(
        {"buffer": wav_bytes, "mimetype": "audio/wav"},
        options,
    )

    # Extract transcript and detected language
    if (response and
        response.results and
        response.results.channels and
        response.results.channels[0].alternatives):

        alt = response.results.channels[0].alternatives[0]
        transcript = alt.transcript.strip()

        # Get detected language
        detected_lang = getattr(
            response.results.channels[0], 'detected_language', None
        ) or settings.stt_language

        # Map language names/codes to our standard codes
        lang_code_map = {
            "english": "en", "hindi": "hi",
            "bengali": "bn", "marathi": "mr",
            "en": "en", "hi": "hi", "bn": "bn", "mr": "mr",
        }
        detected_lang = lang_code_map.get(
            detected_lang.lower() if isinstance(detected_lang, str) else "",
            settings.stt_language
        )

        return transcript, detected_lang

    return "", settings.stt_language


def _transcribe_rest(wav_bytes: bytes) -> tuple[str, str]:
    """Fallback: raw REST API call (no SDK)."""
    import httpx

    headers = {
        "Authorization": f"Token {settings.deepgram_api_key}",
        "Content-Type": "audio/wav",
    }
    params = {
        "model": settings.stt_model,
        "language": "multi",
        "smart_format": "true",
    }

    response = httpx.post(
        "https://api.deepgram.com/v1/listen",
        headers=headers,
        params=params,
        content=wav_bytes,
        timeout=10.0,
    )
    response.raise_for_status()
    result = response.json()

    channels = result.get("results", {}).get("channels", [])
    if not channels:
        return "", "en"

    alt = channels[0].get("alternatives", [{}])[0]
    transcript = alt.get("transcript", "")
    detected_lang = channels[0].get("detected_language", settings.stt_language)

    # Map language codes
    lang_code_map = {
        "english": "en", "hindi": "hi",
        "bengali": "bn", "marathi": "mr",
    }
    if isinstance(detected_lang, str) and detected_lang.lower() in lang_code_map:
        detected_lang = lang_code_map[detected_lang.lower()]

    return transcript, detected_lang
