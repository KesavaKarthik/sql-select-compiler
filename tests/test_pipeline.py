"""End-to-end tests: the full pipeline, the schema abstraction, and the CLI."""

import os

import pytest

import cli
from sql_analyzer import (
    ColumnSchema,
    InMemorySchemaProvider,
    LexError,
    MySQLSchemaProvider,
    ParseError,
    SemanticError,
    TableSchema,
    analyze,
)


# -- analyze() -------------------------------------------------------------

def test_full_pipeline_generates_a_bean(schema):
    bean = analyze(
        "SELECT b.seat AS seatNumber, b.price, f.destination, COUNT(b.id) AS total "
        "FROM bookings b "
        "JOIN flights f ON b.flight_id = f.id "
        "WHERE b.price + 10 > 100 "
        "GROUP BY f.destination",
        schema,
        class_name="BookingSummary",
    )
    assert "public class BookingSummary {" in bean
    assert "private String seatNumber;" in bean
    assert "private BigDecimal price;" in bean
    assert "private String destination;" in bean
    assert "private int total;" in bean


def test_default_class_name(schema):
    assert "public class QueryResult {" in analyze("SELECT seat FROM bookings", schema)


def test_syntax_errors_surface_as_parse_error(schema):
    with pytest.raises(ParseError):
        analyze("SELECT FROM bookings", schema)


def test_lex_errors_surface_as_lex_error(schema):
    with pytest.raises(LexError):
        analyze("SELECT 'unterminated FROM bookings", schema)


def test_semantic_errors_expose_the_individual_problems(schema):
    with pytest.raises(SemanticError) as excinfo:
        analyze("SELECT id FROM bookings b JOIN flights f ON b.flight_id = f.id", schema)
    assert excinfo.value.errors, "SemanticError should carry a structured error list"
    assert any("ambiguous" in e for e in excinfo.value.errors)


def test_semantic_error_is_still_a_value_error(schema):
    """Subclassing ValueError keeps older callers working."""
    with pytest.raises(ValueError):
        analyze("SELECT nope FROM bookings", schema)


# -- the SchemaProvider abstraction ----------------------------------------

def test_in_memory_provider_lookup_is_case_insensitive():
    provider = InMemorySchemaProvider([
        TableSchema("Bookings", [ColumnSchema("Id", "int", None, None, False)])
    ])
    assert provider.get_table("bookings") is not None
    assert provider.get_table("BOOKINGS").get_column("ID") is not None


def test_in_memory_provider_returns_none_for_unknown_table():
    assert InMemorySchemaProvider([]).get_table("ghost") is None


def test_swapping_the_provider_changes_nothing_upstream():
    """The point of the interface: a different schema source, same pipeline."""
    other = InMemorySchemaProvider([
        TableSchema("users", [ColumnSchema("email", "varchar", 255, None, True)])
    ])
    assert "private String email;" in analyze("SELECT email FROM users", other)


@pytest.mark.parametrize("column_type,expected", [
    ("varchar(255)", (255, None)),
    ("decimal(10,2)", (10, 2)),
    ("int", (None, None)),
    ("datetime", (None, None)),
])
def test_mysql_column_type_parsing(column_type, expected):
    """Parsed without a database: it is pure string handling."""
    assert MySQLSchemaProvider._parse_size(column_type) == expected


def test_from_env_reads_configuration(monkeypatch):
    monkeypatch.setenv("MYSQL_PASSWORD", "secret")
    monkeypatch.setenv("MYSQL_DATABASE", "flights")
    monkeypatch.setenv("MYSQL_HOST", "db.internal")
    provider = MySQLSchemaProvider.from_env()
    assert provider._conn_params["host"] == "db.internal"
    assert provider._conn_params["user"] == "root"  # default


def test_from_env_names_the_missing_variable(monkeypatch):
    monkeypatch.delenv("MYSQL_PASSWORD", raising=False)
    monkeypatch.delenv("MYSQL_DATABASE", raising=False)
    with pytest.raises(KeyError, match="MYSQL_PASSWORD"):
        MySQLSchemaProvider.from_env()


# -- the CLI ---------------------------------------------------------------

def test_cli_demo_succeeds(capsys):
    assert cli.main(["--demo"]) == cli.EXIT_OK
    assert "public class QueryResult {" in capsys.readouterr().out


def test_cli_honours_class_name(capsys):
    cli.main(["--demo", "--sql", "SELECT seat FROM bookings", "-c", "Seat"])
    assert "public class Seat {" in capsys.readouterr().out


def test_cli_reports_syntax_errors(capsys):
    code = cli.main(["--demo", "--sql", "SELECT FROM bookings"])
    assert code == cli.EXIT_SYNTAX
    assert "syntax error" in capsys.readouterr().err


def test_cli_reports_semantic_errors(capsys):
    code = cli.main(["--demo", "--sql", "SELECT ghost FROM bookings"])
    assert code == cli.EXIT_SEMANTIC
    assert "Semantic validation failed" in capsys.readouterr().err


def test_cli_writes_to_a_file(tmp_path, capsys):
    out = tmp_path / "Bean.java"
    code = cli.main(["--demo", "--sql", "SELECT seat FROM bookings", "-o", str(out)])
    assert code == cli.EXIT_OK
    assert "public class QueryResult {" in out.read_text(encoding="utf-8")


def test_cli_reads_from_a_file(tmp_path, capsys):
    sql_file = tmp_path / "query.sql"
    sql_file.write_text("SELECT seat FROM bookings", encoding="utf-8")
    assert cli.main(["--demo", "-f", str(sql_file)]) == cli.EXIT_OK
    assert "private String seat;" in capsys.readouterr().out


def test_cli_rejects_an_unreadable_file(tmp_path, capsys):
    missing = tmp_path / "nope.sql"
    assert cli.main(["--demo", "-f", str(missing)]) == cli.EXIT_USAGE
    assert "cannot read" in capsys.readouterr().err


def test_cli_without_database_config_explains_itself(monkeypatch, capsys):
    monkeypatch.delenv("MYSQL_PASSWORD", raising=False)
    monkeypatch.delenv("MYSQL_DATABASE", raising=False)
    code = cli.main(["--sql", "SELECT seat FROM bookings"])
    assert code == cli.EXIT_USAGE
    assert "--demo" in capsys.readouterr().err


def test_stdin_is_read_only_when_explicitly_requested(monkeypatch, capsys):
    """"-f -" is the only way in: anything else must not touch stdin."""
    import io as _io
    monkeypatch.setattr(cli.sys, "stdin", _io.StringIO("SELECT b.seat FROM bookings b"))
    assert cli.main(["--demo", "-f", "-"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "private String seat;" in out
    assert "destination" not in out, "the sample query must not have been used"


def test_demo_never_touches_stdin(monkeypatch, capsys):
    """
    Regression: reading stdin unasked hangs forever under a launcher that
    leaves it open and idle, which is how IDE run buttons behave.
    """
    class Forbidden:
        def isatty(self): raise AssertionError("stdin must not be probed")
        def read(self, *a): raise AssertionError("stdin must not be read")
    monkeypatch.setattr(cli.sys, "stdin", Forbidden())
    assert cli.main(["--demo"]) == cli.EXIT_OK
    assert "private String destination;" in capsys.readouterr().out


def test_sql_argument_never_touches_stdin(monkeypatch, capsys):
    class Forbidden:
        def isatty(self): raise AssertionError("stdin must not be probed")
        def read(self, *a): raise AssertionError("stdin must not be read")
    monkeypatch.setattr(cli.sys, "stdin", Forbidden())
    assert cli.main(["--demo", "--sql", "SELECT seat FROM bookings"]) == cli.EXIT_OK
    assert "private String seat;" in capsys.readouterr().out


def test_no_input_at_all_is_a_usage_error(monkeypatch, capsys):
    class Forbidden:
        def isatty(self): raise AssertionError("stdin must not be probed")
        def read(self, *a): raise AssertionError("stdin must not be read")
    monkeypatch.setattr(cli.sys, "stdin", Forbidden())
    monkeypatch.setenv("MYSQL_PASSWORD", "x")
    monkeypatch.setenv("MYSQL_DATABASE", "y")
    assert cli.main([]) == cli.EXIT_USAGE
    assert "no SQL provided" in capsys.readouterr().err
