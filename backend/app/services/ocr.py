"""OCR service using Google Cloud Vision API, with Tesseract fallback."""

import logging
import os

logger = logging.getLogger(__name__)


def ocr_image_vision(image_path: str) -> str:
    """Extract text from an image using Google Cloud Vision API."""
    from google.cloud import vision

    client = vision.ImageAnnotatorClient()

    with open(image_path, "rb") as f:
        content = f.read()

    image = vision.Image(content=content)
    response = client.document_text_detection(image=image)

    if response.error.message:
        raise RuntimeError(f"Vision API error: {response.error.message}")

    text = response.full_text_annotation.text if response.full_text_annotation else ""
    return text.strip()


def ocr_image_vision_bytes(image_bytes: bytes) -> str:
    """Extract text from image bytes using Google Cloud Vision API."""
    from google.cloud import vision

    client = vision.ImageAnnotatorClient()
    image = vision.Image(content=image_bytes)
    response = client.document_text_detection(image=image)

    if response.error.message:
        raise RuntimeError(f"Vision API error: {response.error.message}")

    text = response.full_text_annotation.text if response.full_text_annotation else ""
    return text.strip()


def ocr_image_tesseract(image_path: str) -> str:
    """Fallback: extract text from an image using Tesseract."""
    import pytesseract
    from PIL import Image

    img = Image.open(image_path)
    text = pytesseract.image_to_string(img)
    return text.strip() if text else ""


def ocr_image(image_path: str, use_vision: bool = True) -> str:
    """Extract text from an image. Uses Cloud Vision if available, falls back to Tesseract."""
    if use_vision:
        try:
            text = ocr_image_vision(image_path)
            if text:
                return text
        except Exception as e:
            logger.warning("Cloud Vision OCR failed, falling back to Tesseract: %s", e)

    return ocr_image_tesseract(image_path)
