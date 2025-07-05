"""Shared fixtures.

Everything here runs against InMemorySchemaProvider, so the whole suite is
hermetic: no MySQL server, no network, no fixtures to tear down.
"""

import sys
from pathlib import Path

import pytest

# Make the project root importable when pytest is run from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sql_analyzer import (  # noqa: E402
    ColumnSchema,
    InMemorySchemaProvider,
    Lexer,
    Parser,
    TableSchema,
    demo_schema,
)


@pytest.fixture
def schema():
    """The two-table bookings/flights schema shared with the demo."""
    return demo_schema()


@pytest.fixture
def single_table_schema():
    """One table only - useful when a test must avoid ambiguity across joins."""
    return InMemorySchemaProvider([
        TableSchema("bookings", [
            ColumnSchema("id", "int", None, None, False),
            ColumnSchema("seat", "varchar", 8, None, True),
            ColumnSchema("price", "decimal", 10, 2, False),
        ])
    ])


@pytest.fixture
def parse():
    """Parse SQL straight to an AST, skipping the schema entirely."""
    def _parse(sql: str):
        return Parser(Lexer(sql).tokenize()).parse()
    return _parse
