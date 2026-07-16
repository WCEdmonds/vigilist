"""Tests for the full-text search query builder.

build_tsquery output is passed to Postgres to_tsquery() as a bound
parameter — these tests guard two invariants:
1. No input can smuggle quotes/backslashes into the output (defense in
   depth on top of the bound parameter).
2. The output is always a syntactically valid tsquery expression
   (no dangling operators), so arbitrary user input can never 500 a search.
"""

import random
import string

from app.services.search import build_tsquery


def test_basic_words_joined_with_and():
    assert build_tsquery("breach of contract") == "breach & of & contract"


def test_quoted_phrase():
    assert build_tsquery('"breach of contract"') == "(breach <-> of <-> contract)"


def test_boolean_operators():
    assert build_tsquery("settlement AND smith OR jones") == "settlement & smith | jones"


def test_prefix_wildcard():
    assert build_tsquery("deposition* AND damage*") == "deposition:* & damage:*"


def test_not_prefixes_operand():
    assert build_tsquery("NOT privileged") == "!privileged"
    assert build_tsquery("smith NOT jones") == "smith & !jones"


def test_apostrophes_stripped():
    # A quote in a lexeme used to break the tsquery string entirely.
    assert build_tsquery("O'Brien") == "obrien"


def test_sql_injection_characters_removed():
    out = build_tsquery("'; DROP TABLE documents; --")
    assert "'" not in out
    assert ";" not in out
    assert out == "drop & table & documents"


def test_dangling_operators_dropped():
    assert build_tsquery("foo AND") == "foo"
    assert build_tsquery("AND foo") == "foo"
    assert build_tsquery("OR") == ""
    assert build_tsquery("NOT") == ""


def test_punctuation_only_tokens_dropped():
    assert build_tsquery("!!! - ()") == ""


def test_hyphenated_words_preserved():
    assert build_tsquery("covid-19") == "covid-19"


def test_raw_tsquery_operators_stripped_from_lexemes():
    # Users typing raw & | ! chars must not produce operator injection.
    assert build_tsquery("a & b | c") == "a & b & c"


def test_fuzz_no_quotes_or_backslashes():
    rng = random.Random(42)
    for _ in range(500):
        s = "".join(rng.choice(string.printable) for _ in range(rng.randint(1, 40)))
        out = build_tsquery(s)
        assert "'" not in out
        assert "\\" not in out
        # No dangling binary operators at either end
        assert not out.startswith(("&", "|"))
        assert not out.endswith(("&", "|", "!"))
