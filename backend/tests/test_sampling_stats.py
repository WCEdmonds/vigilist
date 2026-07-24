"""Pure tests for defensible-sampling statistics (P3-2)."""

import pytest

from app.services.sampling_stats import acceptance, sample_size, wilson_ci


# --- sample_size ------------------------------------------------------------

def test_classic_95_5_large_population():
    assert sample_size(100_000, 95, 0.05) == 383


def test_finite_population_correction_shrinks_n():
    assert sample_size(500, 95, 0.05) == 218   # much less than 384


def test_never_exceeds_population():
    assert sample_size(50, 95, 0.05) <= 50


def test_expected_rate_shrinks_n():
    n_conservative = sample_size(100_000, 95, 0.05, 0.5)
    n_low_prevalence = sample_size(100_000, 95, 0.05, 0.1)
    assert n_low_prevalence < n_conservative


def test_tighter_margin_grows_n():
    assert sample_size(1_000_000, 95, 0.02) > sample_size(1_000_000, 95, 0.05)


def test_sample_size_validation():
    with pytest.raises(ValueError):
        sample_size(0)
    with pytest.raises(ValueError):
        sample_size(100, margin=0)
    with pytest.raises(ValueError):
        sample_size(100, expected_rate=1.0)
    with pytest.raises(ValueError):
        sample_size(100, confidence=80)


# --- wilson_ci --------------------------------------------------------------

def test_wilson_known_value():
    rate, low, high = wilson_ci(10, 100, 95)
    assert rate == 0.10
    assert 0.054 < low < 0.056        # ~0.0552
    assert 0.174 < high < 0.176       # ~0.1744


def test_wilson_zero_positives_lower_bound_zero():
    rate, low, high = wilson_ci(0, 100, 95)
    assert (rate, low) == (0.0, 0.0)
    assert 0.03 < high < 0.05         # ~0.037: "we saw none" still bounds risk


def test_wilson_all_positive_upper_bound_one():
    rate, low, high = wilson_ci(50, 50, 95)
    assert (rate, high) == (1.0, 1.0)
    assert low > 0.9


def test_wilson_empty_sample():
    assert wilson_ci(0, 0) == (0.0, 0.0, 0.0)


def test_wilson_validation():
    with pytest.raises(ValueError):
        wilson_ci(5, 4)


# --- acceptance -------------------------------------------------------------

def test_acceptance_clean_sample_accepts():
    out = acceptance(0, 200, tolerable_rate=0.05)
    assert out["accept"] is True
    assert out["upper_bound"] < 0.05


def test_acceptance_dirty_sample_rejects():
    out = acceptance(20, 200, tolerable_rate=0.05)
    assert out["accept"] is False
    assert out["upper_bound"] > 0.05


def test_acceptance_empty_sample_never_accepts():
    assert acceptance(0, 0, tolerable_rate=0.05)["accept"] is False


def test_acceptance_validation():
    with pytest.raises(ValueError):
        acceptance(0, 10, tolerable_rate=0)
