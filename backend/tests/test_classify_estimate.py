"""Classification cost estimation math."""

from app.routers.review import estimate_classification_cost


def test_estimate_scales_with_count():
    out = estimate_classification_cost(1000, 8000.0)
    per_doc_in = 8000 / 4 + 800  # 2800
    assert out["doc_count"] == 1000
    assert out["est_input_tokens"] == int(per_doc_in * 1000)
    assert out["est_output_tokens"] == 300 * 1000
    assert out["est_usd"] == round((per_doc_in * 1000 * 3 + 300 * 1000 * 15) / 1_000_000, 2)


def test_estimate_caps_at_truncation_limit():
    capped = estimate_classification_cost(10, 50000.0)
    assert capped["est_input_tokens"] == int((12000 / 4 + 800) * 10)


def test_zero_docs():
    out = estimate_classification_cost(0, 0)
    assert out["est_usd"] == 0.0
