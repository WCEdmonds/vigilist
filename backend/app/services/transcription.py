"""Audio/video transcription using OpenAI Whisper API."""

import logging
import os
import subprocess
import tempfile

from openai import OpenAI

from app.config import settings

logger = logging.getLogger(__name__)

MAX_FILE_SIZE = 24 * 1024 * 1024  # 24MB (Whisper limit is 25MB, leave margin)


def _extract_audio(video_path: str) -> str | None:
    """Extract audio from video file as MP3 using ffmpeg.

    Returns path to extracted audio file, or None if ffmpeg fails.
    """
    fd, audio_path = tempfile.mkstemp(suffix=".mp3")
    os.close(fd)
    try:
        result = subprocess.run(
            ["ffmpeg", "-i", video_path, "-vn", "-acodec", "libmp3lame",
             "-ab", "64k", "-ar", "16000", "-ac", "1", "-y", audio_path],
            capture_output=True, timeout=300,
        )
        if result.returncode != 0:
            logger.warning("ffmpeg failed: %s", result.stderr[:500])
            os.unlink(audio_path)
            return None
        return audio_path
    except Exception as e:
        logger.warning("ffmpeg error: %s", e)
        if os.path.exists(audio_path):
            os.unlink(audio_path)
        return None


def _split_audio(audio_path: str, max_size: int = MAX_FILE_SIZE) -> list[str]:
    """Split audio file into chunks under max_size using ffmpeg.

    Returns list of chunk file paths.
    """
    file_size = os.path.getsize(audio_path)
    if file_size <= max_size:
        return [audio_path]

    # Estimate duration and split
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", audio_path],
            capture_output=True, text=True, timeout=30,
        )
        duration = float(result.stdout.strip())
    except Exception:
        duration = 600  # fallback: assume 10 min

    # Calculate chunk duration to stay under max_size
    num_chunks = int(file_size / max_size) + 1
    chunk_duration = duration / num_chunks

    chunks = []
    for i in range(num_chunks):
        fd, chunk_path = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)
        start = i * chunk_duration
        subprocess.run(
            ["ffmpeg", "-i", audio_path, "-ss", str(start), "-t", str(chunk_duration),
             "-acodec", "copy", "-y", chunk_path],
            capture_output=True, timeout=120,
        )
        if os.path.exists(chunk_path) and os.path.getsize(chunk_path) > 0:
            chunks.append(chunk_path)
        else:
            if os.path.exists(chunk_path):
                os.unlink(chunk_path)

    return chunks


def transcribe_file(local_path: str) -> str | None:
    """Transcribe an audio/video file using Whisper API.

    Handles large files by extracting audio and splitting into chunks.
    Returns the transcription text, or None if transcription fails.
    """
    if not settings.openai_api_key:
        logger.warning("OpenAI API key not configured, skipping transcription")
        return None

    client = OpenAI(api_key=settings.openai_api_key)
    audio_path = None
    chunks_to_clean = []

    try:
        file_size = os.path.getsize(local_path)

        # If file is too large or is a video, extract audio first
        ext = os.path.splitext(local_path)[1].lower()
        if file_size > MAX_FILE_SIZE or ext in ('.mp4', '.mov', '.avi', '.mkv', '.webm'):
            logger.info("Extracting audio from %s (%d MB)", os.path.basename(local_path), file_size // (1024*1024))
            audio_path = _extract_audio(local_path)
            if not audio_path:
                logger.warning("Failed to extract audio from %s", local_path)
                return None
            work_path = audio_path
        else:
            work_path = local_path

        # Split if still too large
        chunks = _split_audio(work_path)
        if len(chunks) > 1:
            chunks_to_clean = chunks
            logger.info("Split into %d chunks for %s", len(chunks), os.path.basename(local_path))

        # Transcribe each chunk
        all_text = []
        for chunk_path in chunks:
            try:
                with open(chunk_path, "rb") as f:
                    response = client.audio.transcriptions.create(
                        model="whisper-1",
                        file=f,
                        response_format="text",
                    )
                text = response.strip() if isinstance(response, str) else str(response).strip()
                if text:
                    all_text.append(text)
            except Exception as e:
                logger.warning("Chunk transcription failed: %s", e)

        full_text = " ".join(all_text)
        if full_text:
            logger.info("Transcribed %s: %d chars", os.path.basename(local_path), len(full_text))
        return full_text if full_text else None

    except Exception as e:
        logger.error("Transcription failed for %s: %s", local_path, e)
        return None
    finally:
        if audio_path and os.path.exists(audio_path):
            os.unlink(audio_path)
        for chunk in chunks_to_clean:
            if os.path.exists(chunk):
                os.unlink(chunk)
