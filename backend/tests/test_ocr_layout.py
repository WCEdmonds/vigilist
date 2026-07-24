"""Unit tests for layout-aware parsing of Vision full_text_annotation."""

import json
from types import SimpleNamespace as NS

from app.services.ocr import (
    PageOcr,
    page_ocr_from_annotation,
    sidecar_bytes,
    sidecar_remote_path,
)

# Vision TextAnnotation.DetectedBreak.BreakType enum ints
SPACE, SURE_SPACE, EOL_SURE_SPACE, HYPHEN, LINE_BREAK = 1, 2, 3, 4, 5


def _word(text, box, brk=SPACE, confidence=0.9):
    """One Vision word: one symbol per char, detected_break on the last."""
    symbols = [NS(text=c, property=None) for c in text]
    symbols[-1] = NS(
        text=text[-1],
        property=NS(detected_break=NS(type_=brk)),
    )
    x0, y0, x1, y1 = box
    vertices = [NS(x=x0, y=y0), NS(x=x1, y=y0), NS(x=x1, y=y1), NS(x=x0, y=y1)]
    return NS(symbols=symbols, bounding_box=NS(vertices=vertices), confidence=confidence)


def _annotation(blocks, width=1000, height=2000, text="fallback"):
    page = NS(width=width, height=height, blocks=blocks)
    return NS(pages=[page], text=text)


def _para(*words):
    return NS(words=list(words))


def _block(*paras):
    return NS(paragraphs=list(paras))


def test_words_joined_with_spaces_and_line_breaks():
    fta = _annotation([
        _block(_para(
            _word("Dear", (0, 0, 100, 20)),
            _word("counsel,", (110, 0, 300, 20), brk=LINE_BREAK),
            _word("hello", (0, 30, 100, 50), brk=LINE_BREAK),
        )),
    ])
    page = page_ocr_from_annotation(fta)
    assert page.text == "Dear counsel,\nhello"


def test_paragraphs_newline_blocks_blank_line():
    fta = _annotation([
        _block(
            _para(_word("Para1", (0, 0, 50, 10), brk=LINE_BREAK)),
            _para(_word("Para2", (0, 20, 50, 30), brk=LINE_BREAK)),
        ),
        _block(_para(_word("Block2", (0, 100, 50, 110), brk=LINE_BREAK))),
    ])
    page = page_ocr_from_annotation(fta)
    assert page.text == "Para1\nPara2\n\nBlock2"


def test_hyphen_break_joins_without_space():
    fta = _annotation([
        _block(_para(
            _word("privi", (0, 0, 50, 10), brk=HYPHEN),
            _word("leged", (0, 12, 50, 22), brk=LINE_BREAK),
        )),
    ])
    page = page_ocr_from_annotation(fta)
    assert page.text == "privi-leged"


def test_word_boxes_are_percent_envelope():
    # 1000x2000 page; box 100..300 x, 200..400 y -> x=10%, y=10%, w=20%, h=10%
    fta = _annotation([
        _block(_para(_word("Hi", (100, 200, 300, 400), confidence=0.987))),
    ])
    page = page_ocr_from_annotation(fta)
    assert page.width == 1000 and page.height == 2000
    assert page.words == [
        {"t": "Hi", "x": 10.0, "y": 10.0, "w": 20.0, "h": 10.0, "c": 0.99}
    ]


def test_skewed_vertices_use_min_max_envelope():
    # Rotated quad: envelope is min/max over all four corners
    vertices = [NS(x=110, y=200), NS(x=300, y=210), NS(x=290, y=400), NS(x=100, y=390)]
    word = NS(
        symbols=[NS(text="X", property=NS(detected_break=NS(type_=LINE_BREAK)))],
        bounding_box=NS(vertices=vertices),
        confidence=0.5,
    )
    fta = _annotation([_block(_para(word))])
    box = page_ocr_from_annotation(fta).words[0]
    assert (box["x"], box["y"], box["w"], box["h"]) == (10.0, 10.0, 20.0, 10.0)


def test_out_of_page_coords_clamped_degenerate_dropped():
    fta = _annotation([
        _block(_para(
            _word("clamped", (-50, -50, 500, 100)),          # clamps to 0..50%, 0..5%
            _word("gone", (999999, 0, 999999, 0), brk=LINE_BREAK),  # degenerate -> dropped
        )),
    ])
    page = page_ocr_from_annotation(fta)
    assert [w["t"] for w in page.words] == ["clamped"]
    w = page.words[0]
    assert w["x"] == 0.0 and w["y"] == 0.0 and w["w"] == 50.0 and w["h"] == 5.0
    # dropped from words but still present in text
    assert "gone" in page.text


def test_no_page_structure_falls_back_to_flat_text():
    fta = NS(pages=[], text="  flat text  ")
    page = page_ocr_from_annotation(fta)
    assert page == PageOcr(text="flat text", words=[], width=0, height=0)


def test_none_annotation_yields_empty():
    assert page_ocr_from_annotation(None) == PageOcr()


def test_sidecar_bytes_shape():
    page = PageOcr(text="ignored", words=[{"t": "a", "x": 1.0, "y": 2.0, "w": 3.0, "h": 4.0, "c": 0.9}], width=10, height=20)
    payload = json.loads(sidecar_bytes(page))
    assert payload == {
        "v": 1, "width": 10, "height": 20,
        "words": [{"t": "a", "x": 1.0, "y": 2.0, "w": 3.0, "h": 4.0, "c": 0.9}],
    }


def test_sidecar_remote_path():
    assert sidecar_remote_path(7, "SMITH_000001_0001") == "productions/7/ocr/SMITH_000001_0001.json"


def test_rounding_preserves_rect_bounds():
    """Regression: x + w and y + h must respect 100.0 boundary after rounding.

    Fractional pixels near a clamped edge can cause independent rounding of
    endpoints and dimensions to violate x + w <= 100 and y + h <= 100.
    This test reproduces the case where x0=10.005, x1=100.0 (right-edge clamp):
    old code would round x=10.01, w=90.0, giving x+w=100.01 (invalid).
    New code rounds endpoints first, derives w from rounded endpoints.
    """
    # Create a word with fractional coordinates that clamp to page bounds
    # x: 10.05 px to 1000.0 px on a 1000 px page -> 10.005% to 100.0%
    # y: 100.25 px to 2000.0 px on a 2000 px page -> 5.0125% to 100.0%
    fta = _annotation([
        _block(_para(_word("flush", (10.05, 100.25, 1000.0, 2000.0)))),
    ], width=1000, height=2000)
    page = page_ocr_from_annotation(fta)
    assert len(page.words) == 1
    box = page.words[0]
    # Verify that x + w <= 100.0 and y + h <= 100.0 (no rounding overflow)
    assert box["x"] + box["w"] <= 100.0, f"x + w = {box['x']} + {box['w']} = {box['x'] + box['w']} > 100.0"
    assert box["y"] + box["h"] <= 100.0, f"y + h = {box['y']} + {box['h']} = {box['y'] + box['h']} > 100.0"
