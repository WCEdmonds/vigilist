"""Pixel-level tests for the pure redaction burn-in service (P1-2/3)."""

from dataclasses import dataclass

from PIL import Image

from app.services.redaction_render import burn_page


@dataclass
class Rect:
    x_pct: float
    y_pct: float
    w_pct: float
    h_pct: float
    reason_code: str = "pii"


def _white(w=1000, h=1000):
    return Image.new("RGB", (w, h), "white")


def test_pixels_inside_rect_are_black_outside_untouched():
    img = _white()
    out = burn_page(img, [Rect(10, 10, 20, 20)])
    # dead center of the box (20%, 20%) -> (200, 200)
    assert out.getpixel((200, 200)) == (0, 0, 0)
    # well outside the box
    assert out.getpixel((500, 500)) == (255, 255, 255)
    assert out.getpixel((50, 50)) == (255, 255, 255)


def test_input_image_not_mutated():
    img = _white()
    burn_page(img, [Rect(0, 0, 50, 50)])
    assert img.getpixel((100, 100)) == (255, 255, 255)


def test_multiple_and_overlapping_rects():
    img = _white()
    out = burn_page(img, [Rect(0, 0, 30, 30), Rect(20, 20, 30, 30), Rect(60, 60, 10, 10)])
    assert out.getpixel((100, 100)) == (0, 0, 0)    # first rect
    assert out.getpixel((250, 250)) == (0, 0, 0)    # overlap zone
    assert out.getpixel((650, 650)) == (0, 0, 0)    # third rect
    assert out.getpixel((990, 990)) == (255, 255, 255)


def test_edge_hugging_rect_stays_in_bounds():
    img = _white()
    out = burn_page(img, [Rect(80, 90, 20, 10)])  # x+w = 100, y+h = 100
    assert out.size == (1000, 1000)
    assert out.getpixel((999, 999)) == (0, 0, 0)
    assert out.getpixel((799, 899)) == (255, 255, 255)


def test_label_renders_in_large_box():
    img = _white()
    out = burn_page(img, [Rect(10, 10, 60, 20, reason_code="attorney_client")])
    # label is white text inside the black box -> some non-black pixels inside
    box = out.crop((100, 100, 700, 300))
    colors = box.getcolors(maxcolors=1_000_000)
    non_black = [c for c in colors if c[1] != (0, 0, 0)]
    assert non_black, "expected white label pixels inside large box"


def test_tiny_box_is_solid_black_no_label():
    img = _white()
    out = burn_page(img, [Rect(10, 10, 2, 1, reason_code="attorney_client")])
    box = out.crop((100, 100, 120, 110))
    colors = box.getcolors(maxcolors=1_000_000)
    assert colors is not None and len(colors) == 1 and colors[0][1] == (0, 0, 0)


def test_empty_rects_returns_equal_image():
    img = _white(200, 100)
    out = burn_page(img, [])
    assert out is not img
    assert out.tobytes() == img.tobytes()


def test_non_rgb_input_returns_rgb_even_with_no_rects():
    img = Image.new("RGBA", (100, 100), (255, 255, 255, 255))
    out = burn_page(img, [])
    assert out.mode == "RGB"


def test_out_of_range_rect_is_clamped_not_crashing():
    img = _white(100, 100)
    out = burn_page(img, [Rect(110, 10, 20, 20)])  # starts past the right edge
    assert out.size == (100, 100)
    assert out.getpixel((50, 50)) == (255, 255, 255)


def test_reason_labels_cover_all_reason_codes():
    from app.services.redaction import REDACTION_REASON_CODES
    from app.services.redaction_render import REASON_LABELS
    assert set(REASON_LABELS) == set(REDACTION_REASON_CODES)
