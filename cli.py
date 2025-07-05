"""Command-line interface for the SQL analyzer.

Reads a SELECT statement (from an argument, or a file, or stdin via "-f -"),
validates it
against a schema, and writes the generated Java bean to stdout or a file.

The schema comes either from a live MySQL database (configured through
environment variables) or, with --demo, from the built-in in-memory schema so
the tool can be tried without any database.

Exit codes are distinct so the command composes in scripts:
    0  success
    1  bad usage or unreadable input
    2  SQL failed to lex or parse
    3  SQL parsed but failed semantic validation
    4  a resolved column has no Java type mapping
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional, Sequence

from sql_analyzer import (
    LexError,
    MySQLSchemaProvider,
    ParseError,
    SchemaProvider,
    SemanticError,
    UnmappedTypeError,
    analyze,
    demo_schema,
)

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_SYNTAX = 2
EXIT_SEMANTIC = 3
EXIT_UNMAPPED_TYPE = 4

DEMO_QUERY = (
    "SELECT b.seat AS seatNumber, b.price, f.destination, COUNT(b.id) AS total "
    "FROM bookings b "
    "JOIN flights f ON b.flight_id = f.id "
    "WHERE b.price + 10 > 100 "
    "GROUP BY f.destination"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sql-analyzer",
        description="Generate a Java bean from a SQL SELECT statement.",
        epilog=(
            "MySQL connection is read from the environment: MYSQL_PASSWORD and "
            "MYSQL_DATABASE are required, MYSQL_HOST and MYSQL_USER default to "
            "localhost/root."
        ),
    )

    source = parser.add_mutually_exclusive_group()
    source.add_argument("-s", "--sql", help="the SELECT statement to analyze")
    source.add_argument("-f", "--file", help="read the statement from a file ('-' for stdin)")

    parser.add_argument(
        "-c", "--class-name",
        default="QueryResult",
        help="name of the generated Java class (default: %(default)s)",
    )
    parser.add_argument(
        "-o", "--output",
        help="write the bean to this file instead of stdout",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="use the built-in in-memory schema (and a sample query if none is given)",
    )
    return parser


def read_sql(args: argparse.Namespace) -> Optional[str]:
    """
    Resolve the SQL text: --sql, then --file, then the --demo sample.

    Stdin is only read when asked for explicitly with "-f -". Sniffing it
    instead (isatty() and friends) cannot tell an empty pipe from one that
    has yet to send data, so under a launcher that leaves stdin open and
    idle - IDE run buttons, most CI runners - the read would block forever.
    """
    if args.sql:
        return args.sql
    if args.file:
        if args.file == "-":
            return sys.stdin.read()
        try:
            with open(args.file, encoding="utf-8") as handle:
                return handle.read()
        except OSError as exc:
            print(f"error: cannot read {args.file}: {exc}", file=sys.stderr)
            return None
    if args.demo:
        return DEMO_QUERY
    return None


def build_schema(args: argparse.Namespace) -> Optional[SchemaProvider]:
    if args.demo:
        return demo_schema()
    try:
        return MySQLSchemaProvider.from_env()
    except KeyError as exc:
        print(f"error: {exc.args[0]}", file=sys.stderr)
        print("hint: pass --demo to run without a database.", file=sys.stderr)
        return None


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    sql = read_sql(args)
    if sql is None or not sql.strip():
        print("error: no SQL provided. Use --sql, --file, '-f -' for stdin, "
              "or --demo.", file=sys.stderr)
        return EXIT_USAGE

    schema = build_schema(args)
    if schema is None:
        return EXIT_USAGE

    try:
        bean = analyze(sql, schema, class_name=args.class_name)
    except (LexError, ParseError) as exc:
        print(f"syntax error: {exc}", file=sys.stderr)
        return EXIT_SYNTAX
    except SemanticError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_SEMANTIC
    except UnmappedTypeError as exc:
        print(f"type error: {exc}", file=sys.stderr)
        return EXIT_UNMAPPED_TYPE

    if args.output:
        try:
            with open(args.output, "w", encoding="utf-8") as handle:
                handle.write(bean + "\n")
        except OSError as exc:
            print(f"error: cannot write {args.output}: {exc}", file=sys.stderr)
            return EXIT_USAGE
        print(f"wrote {args.output}", file=sys.stderr)
    else:
        print(bean)

    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
