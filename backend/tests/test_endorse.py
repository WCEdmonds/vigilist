"""Pure Pillow tests for endorsement stamping (P2-2). No DB/network."""

from PIL import Image

from app.services.endorse import (
    SLIP_H,
    SLIP_W,
    page_bates_numbers,
    slip_sheet,
    stamp_page,
)

GRAY = (60, 60, 60)


def _white_count(img, box):
    """Count pure-white pixels inside box=(l, t, r, b)."""
    data = img.crop(box).tobytes()
    return sum(1 for i in range(0, len(data), 3) if data[i:i + 3] == b"\xff\xff\xff")


# --- page_bates_numbers -----------------------------------------------------

def test_page_bates_sequence():
    out = page_bates_numbers("SMITH000005", "SMITH", 6, 3)
    assert out == ["SMITH000005", "SMITH000006", "SMITH000007"]


def test_page_bates_single_page():
    assert page_bates_numbers("VOL0100", "VOL", 4, 1) == ["VOL0100"]


def test_page_bates_overflow_grows():
    out = page_bates_numbers("SMITH999999", "SMITH", 6, 2)
    assert out == ["SMITH999999", "SMITH1000000"]


# --- stamp_page -------------------------------------------------------------

def test_stamp_page_returns_copy_and_stamps_bottom_right():
    img = Image.new("RGB", (400, 600), GRAY)
    out = stamp_page(img, "SMITH000001", None)
    assert out is not img
    assert img.getpixel((390, 590)) == GRAY  # original untouched
    h = out.height
    # white backing box appears in the bottom-right strip
    assert _white_count(out, (200, h - 80, 400, h)) > 0
    # nothing stamped bottom-left when designation is None
    assert _white_count(out, (0, h - 80, 200, h)) == 0
    # top of page untouched
    assert _white_count(out, (0, 0, 400, 100)) == 0


def test_stamp_page_designation_bottom_left():
    img = Image.new("RGB", (400, 600), GRAY)
    out = stamp_page(img, "SMITH000001", "CONFIDENTIAL")
    h = out.height
    assert _white_count(out, (0, h - 80, 200, h)) > 0


def test_stamp_page_converts_to_rgb():
    img = Image.new("L", (400, 600), 60)
    out = stamp_page(img, "SMITH000001", None)
    assert out.mode == "RGB"


# --- slip_sheet -------------------------------------------------------------

def test_slip_sheet_dimensions_and_content():
    page = slip_sheet("SMITH000001", "CONFIDENTIAL")
    assert (page.width, page.height) == (SLIP_W, SLIP_H)
    assert page.getpixel((5, 5)) == (255, 255, 255)  # white page
    # title text renders as non-white pixels in the middle band
    mid = page.crop((0, SLIP_H // 2 - 100, SLIP_W, SLIP_H // 2 + 100))
    assert any(b != 255 for b in mid.tobytes())
