"""Stage 2: recursive-descent parsing and operator precedence."""

import pytest

from sql_analyzer import (
    BinaryExpr,
    ColumnRef,
    FunctionCall,
    Literal,
    ParseError,
    Star,
)


def test_select_star(parse):
    stmt = parse("SELECT * FROM bookings")
    assert isinstance(stmt.columns[0].expr, Star)


def test_qualified_and_unqualified_column_refs(parse):
    stmt = parse("SELECT b.seat, price FROM bookings b")
    qualified, plain = (item.expr for item in stmt.columns)
    assert (qualified.qualifier, qualified.column) == ("b", "seat")
    assert (plain.qualifier, plain.column) == (None, "price")


def test_explicit_alias(parse):
    stmt = parse("SELECT seat AS s FROM bookings")
    assert stmt.columns[0].alias == "s"


def test_implicit_table_alias(parse):
    stmt = parse("SELECT seat FROM bookings b")
    assert (stmt.from_tables[0].name, stmt.from_tables[0].alias) == ("bookings", "b")


def test_join_is_captured_with_its_condition(parse):
    stmt = parse(
        "SELECT b.id FROM bookings b JOIN flights f ON b.flight_id = f.id"
    )
    assert len(stmt.joins) == 1
    join = stmt.joins[0]
    assert join.table.name == "flights"
    assert isinstance(join.condition, BinaryExpr)
    assert join.condition.op == "="


@pytest.mark.parametrize("sql", [
    "SELECT b.id FROM bookings b LEFT JOIN flights f ON b.flight_id = f.id",
    "SELECT b.id FROM bookings b INNER JOIN flights f ON b.flight_id = f.id",
    "SELECT b.id FROM bookings b LEFT OUTER JOIN flights f ON b.flight_id = f.id",
])
def test_join_type_modifiers_are_accepted(parse, sql):
    assert len(parse(sql).joins) == 1


def test_multiplication_binds_tighter_than_addition(parse):
    """a + b * c must parse as a + (b * c)."""
    stmt = parse("SELECT a + b * c FROM t")
    root = stmt.columns[0].expr
    assert root.op == "+"
    assert isinstance(root.left, ColumnRef)
    assert root.right.op == "*"


def test_parentheses_override_precedence(parse):
    stmt = parse("SELECT (a + b) * c FROM t")
    root = stmt.columns[0].expr
    assert root.op == "*"
    assert root.left.op == "+"


def test_and_binds_tighter_than_or(parse):
    """a OR b AND c must parse as a OR (b AND c)."""
    stmt = parse("SELECT x FROM t WHERE a OR b AND c")
    assert stmt.where.op == "OR"
    assert stmt.where.right.op == "AND"


def test_comparison_is_looser_than_arithmetic(parse):
    stmt = parse("SELECT x FROM t WHERE price + 10 > 100")
    assert stmt.where.op == ">"
    assert stmt.where.left.op == "+"


def test_function_call_with_star_argument(parse):
    stmt = parse("SELECT COUNT(*) FROM bookings")
    expr = stmt.columns[0].expr
    assert isinstance(expr, FunctionCall)
    assert expr.name == "COUNT"
    assert isinstance(expr.args[0], Star)


def test_function_call_with_nested_expression(parse):
    stmt = parse("SELECT AVG(price * 1.1) FROM bookings")
    inner = stmt.columns[0].expr.args[0]
    assert inner.op == "*"
    assert isinstance(inner.right, Literal)


def test_group_by_and_order_by_are_captured(parse):
    stmt = parse(
        "SELECT destination FROM flights GROUP BY destination ORDER BY destination DESC"
    )
    assert len(stmt.group_by) == 1
    assert len(stmt.order_by) == 1


def test_order_by_accepts_multiple_keys(parse):
    stmt = parse("SELECT a FROM t ORDER BY a ASC, b DESC")
    assert len(stmt.order_by) == 2


@pytest.mark.parametrize("sql,message", [
    ("SELECT FROM bookings", "expected an expression"),
    ("SELECT seat bookings", "expected keyword 'FROM'"),
    ("SELECT seat FROM bookings WHERE", "expected an expression"),
    ("SELECT seat FROM bookings GROUP destination", "expected keyword 'BY'"),
    ("SELECT COUNT(id FROM bookings", "expected ')'"),
])
def test_malformed_sql_is_rejected(parse, sql, message):
    with pytest.raises(ParseError, match=message.replace("(", r"\(").replace(")", r"\)")):
        parse(sql)


def test_trailing_input_is_rejected(parse):
    with pytest.raises(ParseError, match="unexpected trailing input"):
        parse("SELECT seat FROM bookings garbage extra")


def test_parse_errors_report_a_position(parse):
    with pytest.raises(ParseError, match="at position"):
        parse("SELECT FROM bookings")
