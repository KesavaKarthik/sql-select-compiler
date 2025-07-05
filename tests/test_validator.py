"""Stage 4: semantic validation, alias resolution, and type inference."""

import pytest

from sql_analyzer import (
    ColumnRefCollector,
    Lexer,
    Parser,
    SemanticValidator,
    child_nodes,
)


def check(sql, schema):
    stmt = Parser(Lexer(sql).tokenize()).parse()
    return SemanticValidator(schema).validate(stmt)


# -- resolution ------------------------------------------------------------

def test_valid_query_produces_no_errors(schema):
    fields, errors = check("SELECT b.seat FROM bookings b", schema)
    assert errors == []
    assert [f.field_name for f in fields] == ["seat"]


def test_alias_resolves_to_its_table(schema):
    fields, errors = check("SELECT b.price FROM bookings b", schema)
    assert errors == []
    assert fields[0].source_table == "bookings"


def test_table_name_works_without_an_alias(schema):
    fields, errors = check("SELECT bookings.price FROM bookings", schema)
    assert errors == []
    assert fields[0].datatype == "decimal"


def test_unknown_table_is_reported(schema):
    _, errors = check("SELECT x FROM nonexistent", schema)
    assert any("Unknown table 'nonexistent'" in e for e in errors)


def test_unknown_alias_is_reported(schema):
    _, errors = check("SELECT z.seat FROM bookings b", schema)
    assert any("Unknown table alias 'z'" in e for e in errors)


def test_unknown_column_is_reported(schema):
    _, errors = check("SELECT b.nope FROM bookings b", schema)
    assert any("Column 'nope' not found in table 'bookings'" in e for e in errors)


def test_ambiguous_column_across_a_join_is_reported(schema):
    """'id' exists in both bookings and flights, so unqualified use is an error."""
    _, errors = check(
        "SELECT id FROM bookings b JOIN flights f ON b.flight_id = f.id", schema
    )
    assert any("ambiguous" in e for e in errors)


def test_qualifying_the_column_resolves_the_ambiguity(schema):
    _, errors = check(
        "SELECT b.id FROM bookings b JOIN flights f ON b.flight_id = f.id", schema
    )
    assert errors == []


def test_columns_in_where_are_validated_too(schema):
    _, errors = check("SELECT seat FROM bookings WHERE nope > 1", schema)
    assert any("'nope'" in e for e in errors)


def test_columns_in_group_by_are_validated_too(schema):
    _, errors = check("SELECT seat FROM bookings GROUP BY nope", schema)
    assert any("'nope'" in e for e in errors)


def test_errors_are_deduplicated(schema):
    """The same bad column in two clauses must be reported once."""
    _, errors = check("SELECT nope FROM bookings WHERE nope > 1", schema)
    assert len(errors) == len(set(errors))


# -- the SELECT list -------------------------------------------------------

def test_star_expands_to_every_column_of_every_table(schema):
    fields, errors = check("SELECT * FROM bookings", schema)
    assert errors == []
    assert [f.field_name for f in fields] == [
        "id", "passenger_id", "flight_id", "seat", "price",
    ]


def test_star_across_a_join_disambiguates_duplicate_names(schema):
    fields, _ = check(
        "SELECT * FROM bookings b JOIN flights f ON b.flight_id = f.id", schema
    )
    names = [f.field_name for f in fields]
    assert len(names) == len(set(names)), "generated field names must be unique"
    assert "flights_id" in names


def test_alias_becomes_the_field_name(schema):
    fields, _ = check("SELECT b.seat AS seatNumber FROM bookings b", schema)
    assert fields[0].field_name == "seatNumber"


def test_unaliased_expression_gets_a_generated_name(schema):
    fields, _ = check("SELECT COUNT(b.id) FROM bookings b", schema)
    assert fields[0].field_name == "column1"


def test_generated_names_increment(schema):
    fields, _ = check("SELECT COUNT(b.id), SUM(b.price) FROM bookings b", schema)
    assert [f.field_name for f in fields] == ["column1", "column2"]


# -- type inference --------------------------------------------------------

@pytest.mark.parametrize("expr,expected", [
    ("COUNT(b.id)", "int"),
    ("SUM(b.price)", "decimal"),
    ("AVG(b.price)", "decimal"),
    ("MIN(b.seat)", "varchar"),     # MIN/MAX inherit their argument's type
    ("MAX(b.price)", "decimal"),
    ("UNKNOWNFN(b.id)", "varchar"),  # unrecognised functions fall back
])
def test_function_return_types(schema, expr, expected):
    fields, _ = check(f"SELECT {expr} AS v FROM bookings b", schema)
    assert fields[0].datatype == expected


@pytest.mark.parametrize("expr,expected", [
    ("1 + 1", "int"),
    ("1 + 1.5", "decimal"),      # decimal is contagious
    ("b.price + 10", "decimal"),
    ("b.id + 1", "int"),
])
def test_arithmetic_type_inference(schema, expr, expected):
    fields, _ = check(f"SELECT {expr} AS v FROM bookings b", schema)
    assert fields[0].datatype == expected


# -- the visitor infrastructure itself -------------------------------------

def test_column_ref_collector_finds_refs_in_every_clause(schema):
    stmt = Parser(Lexer(
        "SELECT b.seat FROM bookings b JOIN flights f ON b.flight_id = f.id "
        "WHERE b.price > 1 GROUP BY f.destination ORDER BY b.id"
    ).tokenize()).parse()

    collector = ColumnRefCollector()
    collector.visit(stmt)
    found = {(r.qualifier, r.column) for r in collector.refs}

    assert ("b", "seat") in found          # SELECT
    assert ("b", "flight_id") in found     # JOIN condition
    assert ("b", "price") in found         # WHERE
    assert ("f", "destination") in found   # GROUP BY
    assert ("b", "id") in found            # ORDER BY


def test_child_nodes_reflects_over_dataclass_fields(parse):
    """A BinaryExpr's children are discovered without a per-class table."""
    stmt = parse("SELECT a + b FROM t")
    expr = stmt.columns[0].expr
    assert len(child_nodes(expr)) == 2


def test_child_nodes_of_a_leaf_is_empty(parse):
    stmt = parse("SELECT a FROM t")
    assert child_nodes(stmt.columns[0].expr) == []
