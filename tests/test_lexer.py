"""Stage 1: tokenization."""

import pytest

from sql_analyzer import Lexer, LexError, TokenType


def types_and_values(sql):
    return [(t.type, t.value) for t in Lexer(sql).tokenize()]


def test_keywords_are_distinguished_from_identifiers():
    tokens = Lexer("SELECT seat FROM bookings").tokenize()
    kinds = [t.type for t in tokens[:-1]]
    assert kinds == [
        TokenType.KEYWORD,
        TokenType.IDENTIFIER,
        TokenType.KEYWORD,
        TokenType.IDENTIFIER,
    ]


def test_keyword_matching_is_case_insensitive():
    assert Lexer("select").tokenize()[0].type == TokenType.KEYWORD
    assert Lexer("SeLeCt").tokenize()[0].type == TokenType.KEYWORD


def test_stream_always_terminates_with_eof():
    tokens = Lexer("SELECT 1").tokenize()
    assert tokens[-1].type == TokenType.EOF
    assert tokens[-1].value == ""


@pytest.mark.parametrize("op", ["<=", ">=", "<>", "!="])
def test_two_char_operators_are_not_split(op):
    """The greedy two-char rule must win over its single-char prefix."""
    tokens = Lexer(f"a {op} b").tokenize()
    assert (tokens[1].type, tokens[1].value) == (TokenType.OPERATOR, op)


def test_single_char_operator_still_lexes():
    tokens = Lexer("a < b").tokenize()
    assert (tokens[1].type, tokens[1].value) == (TokenType.OPERATOR, "<")


@pytest.mark.parametrize("text,expected", [
    ("10", "10"),
    ("10.5", "10.5"),
])
def test_numbers(text, expected):
    tokens = Lexer(text).tokenize()
    assert (tokens[0].type, tokens[0].value) == (TokenType.NUMBER, expected)


def test_second_dot_ends_the_number():
    """'1.2.3' is a number then a punctuation dot, not one malformed number."""
    tokens = Lexer("1.2.3").tokenize()
    assert tokens[0].value == "1.2"
    assert tokens[1].type == TokenType.PUNCTUATION


def test_string_literal_keeps_its_quotes():
    tokens = Lexer("'hello'").tokenize()
    assert (tokens[0].type, tokens[0].value) == (TokenType.STRING, "'hello'")


def test_unterminated_string_is_rejected():
    with pytest.raises(LexError, match="Unterminated string"):
        Lexer("SELECT 'abc FROM t").tokenize()


def test_unexpected_character_is_rejected():
    with pytest.raises(LexError, match="Unexpected character"):
        Lexer("SELECT # FROM t").tokenize()


def test_whitespace_is_skipped_but_positions_are_kept():
    tokens = Lexer("  SELECT").tokenize()
    assert tokens[0].pos == 2


def test_underscores_are_valid_in_identifiers():
    tokens = Lexer("passenger_id").tokenize()
    assert (tokens[0].type, tokens[0].value) == (TokenType.IDENTIFIER, "passenger_id")
