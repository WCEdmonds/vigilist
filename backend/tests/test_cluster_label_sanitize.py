"""Cluster label sanitation: refusal sentences must never become labels."""

from app.services.clustering import _sanitize_label


def test_real_labels_pass_through():
    assert _sanitize_label("Contract Breach and Cover Purchase") == "Contract Breach and Cover Purchase"
    assert _sanitize_label(" Steel Supply Dispute ") == "Steel Supply Dispute"
    assert _sanitize_label('"Invoices and Billing"') == "Invoices and Billing"


def test_refusal_sentences_fall_back():
    assert _sanitize_label(
        "I cannot reliably extract legible document titles or content"
    ) == "Unlabeled"
    assert _sanitize_label("I'm unable to determine a topic from these excerpts.") == "Unlabeled"
    assert _sanitize_label("Sorry, the text appears to be illegible.") == "Unlabeled"


def test_sentence_shaped_output_falls_back():
    assert _sanitize_label("These documents appear to concern various administrative matters.") == "Unlabeled"
    assert _sanitize_label("x" * 41) == "Unlabeled"
    assert _sanitize_label("") == "Unlabeled"


def test_explicit_unlabeled_stays():
    assert _sanitize_label("Unlabeled") == "Unlabeled"


def test_spread_sample_covers_breadth():
    from app.services.clustering import _spread_sample

    # Fewer items than k: everything is included.
    assert _spread_sample([1, 2, 3], 8) == [1, 2, 3]
    # More items than k: first and last always included, evenly spread.
    sample = _spread_sample(list(range(100)), 8)
    assert len(sample) == 8
    assert sample[0] == 0 and sample[-1] == 99
    assert sample == sorted(sample)
