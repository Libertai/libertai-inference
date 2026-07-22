"""Anon proxy completion-budget cap (shared key pays for the output)."""

from src.routes.chat.proxy import ANON_MAX_OUTPUT_TOKENS, cap_output_tokens


def test_missing_budget_gets_default_cap():
    assert cap_output_tokens({"model": "x"})["max_tokens"] == ANON_MAX_OUTPUT_TOKENS


def test_oversized_budget_is_clamped():
    assert cap_output_tokens({"max_tokens": 200_000})["max_tokens"] == ANON_MAX_OUTPUT_TOKENS


def test_reasonable_budget_is_kept():
    assert cap_output_tokens({"max_tokens": 500})["max_tokens"] == 500


def test_both_spellings_are_clamped():
    capped = cap_output_tokens({"max_tokens": 999_999, "max_completion_tokens": 50_000})
    assert capped["max_tokens"] == ANON_MAX_OUTPUT_TOKENS
    assert capped["max_completion_tokens"] == ANON_MAX_OUTPUT_TOKENS


def test_invalid_budget_replaced_with_default():
    capped = cap_output_tokens({"max_tokens": None, "max_completion_tokens": -5})
    assert capped["max_tokens"] == ANON_MAX_OUTPUT_TOKENS
    assert "max_completion_tokens" not in capped
