"""TIFF to JPEG conversion service."""

import os
from pathlib import Path

from PIL import Image


def convert_tiff_to_jpeg(
    tiff_path: str, output_dir: str, quality: int = 85
) -> str:
    """Convert a single TIFF file to JPEG. Returns the output JPEG path."""
    os.makedirs(output_dir, exist_ok=True)
    stem = Path(tiff_path).stem
    jpeg_path = os.path.join(output_dir, f"{stem}.jpg")

    if os.path.exists(jpeg_path):
        return jpeg_path

    with Image.open(tiff_path) as img:
        # Convert to RGB if needed (TIFFs may be CMYK, palette, etc.)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        img.save(jpeg_path, "JPEG", quality=quality)

    return jpeg_path


def convert_document_images(
    image_paths: list[str],
    production_root: str,
    output_dir: str,
) -> list[str]:
    """Convert all TIFF images for a document to JPEG.

    Args:
        image_paths: Relative paths from the OPT file (already normalized to /).
        production_root: Absolute path to the production root directory.
        output_dir: Directory to write converted JPEGs.

    Returns:
        List of absolute JPEG paths.
    """
    jpeg_paths = []
    for rel_path in image_paths:
        tiff_path = os.path.join(production_root, rel_path)
        if not os.path.exists(tiff_path):
            jpeg_paths.append("")  # placeholder for missing files
            continue
        jpeg_path = convert_tiff_to_jpeg(tiff_path, output_dir)
        jpeg_paths.append(jpeg_path)
    return jpeg_paths
