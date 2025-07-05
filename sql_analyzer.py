"""
SQL SELECT analyzer and Java POJO (bean) generator.

The program takes a SQL SELECT statement, understands its structure, checks it
against a real database schema, and generates a matching Java class.

Four-stage pipeline:

    raw SQL text
      -> Lexer              stream of typed Tokens
      -> Parser             Abstract Syntax Tree
      -> SemanticValidator  resolves aliases, checks columns, produces
                            typed ResolvedFields
      -> CodeGenerator      Java bean for the SELECT list

SchemaProvider is a dependency, not a stage: the validator queries it, nothing
flows through it. MySQLSchemaProvider reads INFORMATION_SCHEMA with credentials
from the environment; InMemorySchemaProvider supplies the same metadata from
plain objects, so the tests need no database.

Limitations:
  - single SELECT only: no subqueries, derived tables, UNION or CTEs
  - expression/function return types use a small inference table
  - WHERE / GROUP BY / ORDER BY are validated but do not shape the bean
"""

from __future__ import annotations

import dataclasses
import os
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Generic, Iterator, Optional, TypeVar


# ---------------------------------------------------------------------------
# Stage 1: Lexer  -  raw text  ->  typed tokens
# ---------------------------------------------------------------------------

class TokenType(Enum):
    KEYWORD = auto()
    IDENTIFIER = auto()
    NUMBER = auto()
    STRING = auto()
    OPERATOR = auto()      # + - * / = < > <= >= <> !=
    PUNCTUATION = auto()   # , . ( )
    EOF = auto()


# Words the lexer classifies as KEYWORD instead of IDENTIFIER. Keeping this
# explicit means "order" or "count" used as a keyword is never mistaken for a
# column name.
KEYWORDS = {
    "SELECT", "FROM", "JOIN", "INNER", "LEFT", "RIGHT", "OUTER", "FULL", "ON",
    "WHERE", "GROUP", "ORDER", "BY", "AS", "AND", "OR", "NOT", "NULL", "IS",
    "IN", "ASC", "DESC",
}


@dataclass
class Token:
    type: TokenType
    value: str
    pos: int   # character offset in the source, used in error messages


class LexError(Exception):
    pass


class Lexer:
    """Turns SQL source text into a flat list of typed tokens."""

    # multi-character operators must be tried before their single-char prefixes
    _TWO_CHAR_OPS = {"<=", ">=", "<>", "!="}
    _ONE_CHAR_OPS = set("+-*/=<>")
    _PUNCTUATION = set(",.()")

    def __init__(self, text: str):
        self.text = text
        self.i = 0
        self.n = len(text)

    def tokenize(self) -> list[Token]:
        tokens: list[Token] = []
        while self.i < self.n:
            ch = self.text[self.i]

            if ch.isspace():
                self.i += 1
                continue

            start = self.i

            if ch.isalpha() or ch == "_":
                tokens.append(self._read_word(start))
            elif ch.isdigit():
                tokens.append(self._read_number(start))
            elif ch == "'":
                tokens.append(self._read_string(start))
            elif self._peek(2) in self._TWO_CHAR_OPS:
                tokens.append(Token(TokenType.OPERATOR, self._peek(2), start))
                self.i += 2
            elif ch in self._ONE_CHAR_OPS:
                tokens.append(Token(TokenType.OPERATOR, ch, start))
                self.i += 1
            elif ch in self._PUNCTUATION:
                tokens.append(Token(TokenType.PUNCTUATION, ch, start))
                self.i += 1
            else:
                raise LexError(f"Unexpected character {ch!r} at position {start}")

        tokens.append(Token(TokenType.EOF, "", self.n))
        return tokens

    def _peek(self, k: int) -> str:
        return self.text[self.i:self.i + k]

    def _read_word(self, start: int) -> Token:
        while self.i < self.n and (self.text[self.i].isalnum() or self.text[self.i] == "_"):
            self.i += 1
        word = self.text[start:self.i]
        ttype = TokenType.KEYWORD if word.upper() in KEYWORDS else TokenType.IDENTIFIER
        return Token(ttype, word, start)

    def _read_number(self, start: int) -> Token:
        seen_dot = False
        while self.i < self.n and (self.text[self.i].isdigit() or self.text[self.i] == "."):
            if self.text[self.i] == ".":
                if seen_dot:
                    break
                seen_dot = True
            self.i += 1
        return Token(TokenType.NUMBER, self.text[start:self.i], start)

    def _read_string(self, start: int) -> Token:
        self.i += 1  # opening quote
        while self.i < self.n and self.text[self.i] != "'":
            self.i += 1
        if self.i >= self.n:
            raise LexError(f"Unterminated string literal starting at position {start}")
        self.i += 1  # closing quote
        return Token(TokenType.STRING, self.text[start:self.i], start)


# ---------------------------------------------------------------------------
# Stage 2: AST node definitions
# ---------------------------------------------------------------------------
# The parser produces a tree of these. Expressions are modelled recursively:
# a FunctionCall holds argument nodes, a BinaryExpr holds a left and right node,
# so "AVG(price * 1.1)" becomes a real nested structure rather than a string.

@dataclass
class Node:
    pass


@dataclass
class Star(Node):
    """The '*' in 'SELECT *' or 'COUNT(*)'."""


@dataclass
class Literal(Node):
    value: str
    is_number: bool


@dataclass
class ColumnRef(Node):
    qualifier: Optional[str]   # the 'a' in 'a.name'; None if unqualified
    column: str


@dataclass
class FunctionCall(Node):
    name: str
    args: list          # list[Node]


@dataclass
class BinaryExpr(Node):
    op: str
    left: Node
    right: Node


@dataclass
class SelectItem(Node):
    expr: Node
    alias: Optional[str]       # the 'x' in 'expr AS x'


@dataclass
class TableRef(Node):
    name: str
    alias: Optional[str]


@dataclass
class JoinClause(Node):
    table: TableRef
    condition: Node


@dataclass
class SelectStatement(Node):
    columns: list        # list[SelectItem]
    from_tables: list    # list[TableRef]
    joins: list          # list[JoinClause]
    where: Optional[Node]
    group_by: list       # list[Node]
    order_by: list       # list[Node]


# ---------------------------------------------------------------------------
# Stage 2 (cont.): Parser  -  tokens  ->  AST
# ---------------------------------------------------------------------------

class ParseError(Exception):
    pass


class Parser:
    """
    A hand-written recursive-descent parser for a SELECT subset. Each grammar
    rule is one method; expression parsing is split by precedence so that
    '*' / '/' bind tighter than '+' / '-'.

    Grammar (informal):
        select_stmt  := SELECT select_list FROM table_list join* where? group? order?
        select_item  := '*' | expression (AS IDENT)?
        expression   := additive
        additive     := multiplicative (('+'|'-') multiplicative)*
        multiplicative := primary (('*'|'/') primary)*
        primary      := NUMBER | STRING | '*' | '(' expression ')'
                        | IDENT '(' args ')'          (function call)
                        | IDENT ('.' IDENT)?          (column reference)
    """

    def __init__(self, tokens: list[Token]):
        self.tokens = tokens
        self.i = 0

    # -- token cursor helpers ------------------------------------------------

    def _peek(self) -> Token:
        return self.tokens[self.i]

    def _advance(self) -> Token:
        tok = self.tokens[self.i]
        self.i += 1
        return tok

    def _at_keyword(self, *names: str) -> bool:
        tok = self._peek()
        return tok.type == TokenType.KEYWORD and tok.value.upper() in names

    def _at_punct(self, value: str) -> bool:
        tok = self._peek()
        return tok.type == TokenType.PUNCTUATION and tok.value == value

    def _at_operator(self, *values: str) -> bool:
        tok = self._peek()
        return tok.type == TokenType.OPERATOR and tok.value in values

    def _expect_keyword(self, name: str) -> Token:
        if not self._at_keyword(name):
            raise ParseError(self._where(f"expected keyword {name!r}"))
        return self._advance()

    def _expect_punct(self, value: str) -> Token:
        if not self._at_punct(value):
            raise ParseError(self._where(f"expected {value!r}"))
        return self._advance()

    def _expect_identifier(self) -> Token:
        if self._peek().type != TokenType.IDENTIFIER:
            raise ParseError(self._where("expected an identifier"))
        return self._advance()

    def _where(self, msg: str) -> str:
        tok = self._peek()
        found = "end of input" if tok.type == TokenType.EOF else f"{tok.value!r}"
        return f"{msg}, but found {found} at position {tok.pos}"

    # -- grammar rules -------------------------------------------------------

    def parse(self) -> SelectStatement:
        self._expect_keyword("SELECT")
        columns = self._parse_select_list()
        self._expect_keyword("FROM")
        from_tables = self._parse_table_list()

        joins: list[JoinClause] = []
        while self._at_keyword("JOIN", "INNER", "LEFT", "RIGHT", "OUTER", "FULL"):
            joins.append(self._parse_join())

        where = None
        if self._at_keyword("WHERE"):
            self._advance()
            where = self._parse_expression()

        group_by: list[Node] = []
        if self._at_keyword("GROUP"):
            self._advance()
            self._expect_keyword("BY")
            group_by = self._parse_expression_list()

        order_by: list[Node] = []
        if self._at_keyword("ORDER"):
            self._advance()
            self._expect_keyword("BY")
            order_by = self._parse_order_list()

        # Nothing should be left over. Trailing tokens mean malformed SQL.
        if self._peek().type != TokenType.EOF:
            raise ParseError(self._where("unexpected trailing input"))

        return SelectStatement(columns, from_tables, joins, where, group_by, order_by)

    def _parse_select_list(self) -> list[SelectItem]:
        items = [self._parse_select_item()]
        while self._at_punct(","):
            self._advance()
            items.append(self._parse_select_item())
        return items

    def _parse_select_item(self) -> SelectItem:
        # A leading '*' means "all columns" rather than multiplication.
        if self._at_operator("*"):
            self._advance()
            return SelectItem(Star(), None)

        expr = self._parse_expression()
        alias = None
        if self._at_keyword("AS"):
            self._advance()
            alias = self._expect_identifier().value
        return SelectItem(expr, alias)

    def _parse_table_list(self) -> list[TableRef]:
        tables = [self._parse_table_ref()]
        while self._at_punct(","):
            self._advance()
            tables.append(self._parse_table_ref())
        return tables

    def _parse_table_ref(self) -> TableRef:
        name = self._expect_identifier().value
        alias = None
        if self._at_keyword("AS"):
            self._advance()
            alias = self._expect_identifier().value
        elif self._peek().type == TokenType.IDENTIFIER:
            # implicit alias, e.g. FROM bookings b
            alias = self._advance().value
        return TableRef(name, alias)

    def _parse_join(self) -> JoinClause:
        # consume any join-type modifiers, then the JOIN keyword itself
        while self._at_keyword("INNER", "LEFT", "RIGHT", "OUTER", "FULL"):
            self._advance()
        self._expect_keyword("JOIN")
        table = self._parse_table_ref()
        self._expect_keyword("ON")
        condition = self._parse_expression()
        return JoinClause(table, condition)

    def _parse_expression_list(self) -> list[Node]:
        exprs = [self._parse_expression()]
        while self._at_punct(","):
            self._advance()
            exprs.append(self._parse_expression())
        return exprs

    def _parse_order_list(self) -> list[Node]:
        exprs = []
        while True:
            exprs.append(self._parse_expression())
            if self._at_keyword("ASC", "DESC"):
                self._advance()
            if self._at_punct(","):
                self._advance()
                continue
            break
        return exprs

    # expression precedence, loosest first:
    #   OR -> AND -> comparison -> additive -> multiplicative -> primary

    def _parse_expression(self) -> Node:
        return self._parse_or()

    def _parse_or(self) -> Node:
        left = self._parse_and()
        while self._at_keyword("OR"):
            op = self._advance().value.upper()
            right = self._parse_and()
            left = BinaryExpr(op, left, right)
        return left

    def _parse_and(self) -> Node:
        left = self._parse_comparison()
        while self._at_keyword("AND"):
            op = self._advance().value.upper()
            right = self._parse_comparison()
            left = BinaryExpr(op, left, right)
        return left

    def _parse_comparison(self) -> Node:
        left = self._parse_additive()
        while self._at_operator("=", "<", ">", "<=", ">=", "<>", "!="):
            op = self._advance().value
            right = self._parse_additive()
            left = BinaryExpr(op, left, right)
        return left

    def _parse_additive(self) -> Node:
        left = self._parse_multiplicative()
        while self._at_operator("+", "-"):
            op = self._advance().value
            right = self._parse_multiplicative()
            left = BinaryExpr(op, left, right)
        return left

    def _parse_multiplicative(self) -> Node:
        left = self._parse_primary()
        while self._at_operator("*", "/"):
            op = self._advance().value
            right = self._parse_primary()
            left = BinaryExpr(op, left, right)
        return left

    def _parse_primary(self) -> Node:
        tok = self._peek()

        if tok.type == TokenType.NUMBER:
            self._advance()
            return Literal(tok.value, is_number=True)

        if tok.type == TokenType.STRING:
            self._advance()
            return Literal(tok.value, is_number=False)

        if self._at_operator("*"):
            # '*' reaching here (e.g. inside COUNT(*)) means "all"
            self._advance()
            return Star()

        if self._at_punct("("):
            self._advance()
            expr = self._parse_expression()
            self._expect_punct(")")
            return expr

        if tok.type == TokenType.IDENTIFIER:
            name = self._advance().value
            if self._at_punct("("):
                return self._parse_function_call(name)
            if self._at_punct("."):
                self._advance()
                column = self._expect_identifier().value
                return ColumnRef(qualifier=name, column=column)
            return ColumnRef(qualifier=None, column=name)

        raise ParseError(self._where("expected an expression"))

    def _parse_function_call(self, name: str) -> FunctionCall:
        self._expect_punct("(")
        args: list[Node] = []
        if not self._at_punct(")"):
            args.append(self._parse_expression())
            while self._at_punct(","):
                self._advance()
                args.append(self._parse_expression())
        self._expect_punct(")")
        return FunctionCall(name, args)


# ---------------------------------------------------------------------------
# Supporting abstraction: the schema interface (not a pipeline stage)
# ---------------------------------------------------------------------------
# The validator depends on this interface, not on MySQL, so any source of
# table metadata can implement it and the pipeline stays testable offline.
# Data does not flow through here, so it is not a stage.

@dataclass
class ColumnSchema:
    name: str
    datatype: str            # e.g. 'varchar', 'int', 'decimal'
    size: Optional[int]
    decimal: Optional[int]
    nullable: bool


@dataclass
class TableSchema:
    name: str
    columns: list            # list[ColumnSchema]

    def get_column(self, name: str) -> Optional[ColumnSchema]:
        for col in self.columns:
            if col.name.lower() == name.lower():
                return col
        return None


class SchemaProvider(ABC):
    @abstractmethod
    def table_names(self) -> list[str]:
        ...

    @abstractmethod
    def get_table(self, name: str) -> Optional[TableSchema]:
        ...


class InMemorySchemaProvider(SchemaProvider):
    """A schema built from plain Python objects, for tests and demos."""

    def __init__(self, tables: list[TableSchema]):
        self._tables = {t.name.lower(): t for t in tables}

    def table_names(self) -> list[str]:
        return [t.name for t in self._tables.values()]

    def get_table(self, name: str) -> Optional[TableSchema]:
        return self._tables.get(name.lower())


class MySQLSchemaProvider(SchemaProvider):
    """
    Reads table and column metadata from INFORMATION_SCHEMA.

    Credentials come from the caller. Metadata is cached on first use, and
    connections are closed through a context manager.
    """

    def __init__(self, host: str, user: str, password: str, database: str):
        self._conn_params = dict(
            host=host, user=user, password=password, database=database
        )
        self._database = database
        # A None value is a cached miss: a table we already know does not exist,
        # so repeated references to it never hit the database again.
        self._cache: dict[str, Optional[TableSchema]] = {}
        self._names: Optional[list[str]] = None

    @classmethod
    def from_env(cls) -> "MySQLSchemaProvider":
        """
        Build a provider from environment variables.

        MYSQL_PASSWORD and MYSQL_DATABASE are required; host and user default
        to localhost/root. Raises KeyError naming the missing variable.
        """
        try:
            password = os.environ["MYSQL_PASSWORD"]
            database = os.environ["MYSQL_DATABASE"]
        except KeyError as exc:
            raise KeyError(
                "Missing required environment variable " + str(exc) + ". "
                "Set MYSQL_PASSWORD and MYSQL_DATABASE before connecting."
            ) from exc
        return cls(
            host=os.environ.get("MYSQL_HOST", "localhost"),
            user=os.environ.get("MYSQL_USER", "root"),
            password=password,
            database=database,
        )

    @contextmanager
    def _cursor(self, dictionary: bool = False) -> Iterator[Any]:
        """Yield a cursor and guarantee the connection is closed afterwards."""
        import mysql.connector  # imported lazily so the module runs without the driver

        conn = mysql.connector.connect(**self._conn_params)
        try:
            yield conn.cursor(dictionary=dictionary)
        finally:
            conn.close()

    def table_names(self) -> list[str]:
        if self._names is None:
            with self._cursor() as cursor:
                cursor.execute(
                    "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
                    "WHERE TABLE_SCHEMA = %s",
                    (self._database,),
                )
                self._names = [row[0] for row in cursor.fetchall()]
        return self._names

    def get_table(self, name: str) -> Optional[TableSchema]:
        key = name.lower()
        if key in self._cache:
            return self._cache[key]

        with self._cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_TYPE "
                "FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s",
                (self._database, name),
            )
            rows = cursor.fetchall()

        if not rows:
            self._cache[key] = None
            return None

        columns = []
        for row in rows:
            size, decimal = self._parse_size(row["COLUMN_TYPE"])
            columns.append(ColumnSchema(
                name=row["COLUMN_NAME"],
                datatype=row["DATA_TYPE"],
                size=size,
                decimal=decimal,
                nullable=(row["IS_NULLABLE"] == "YES"),
            ))
        table = TableSchema(name, columns)
        self._cache[key] = table
        return table

    @staticmethod
    def _parse_size(column_type: str) -> tuple[Optional[int], Optional[int]]:
        # 'varchar(255)' -> (255, None); 'decimal(10,2)' -> (10, 2)
        if "(" not in column_type:
            return None, None
        inside = column_type[column_type.index("(") + 1: column_type.index(")")]
        parts = [q.strip() for q in inside.split(",")]
        try:
            size = int(parts[0])
        except (ValueError, IndexError):
            return None, None
        decimal = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        return size, decimal



# ---------------------------------------------------------------------------
# Supporting machinery: AST traversal  -  the Visitor infrastructure
# ---------------------------------------------------------------------------
# Tree passes (collecting column references, inferring a type) are visitors
# rather than isinstance chains inside the validator, so a new pass is a new
# class instead of an edit to an existing method.

T = TypeVar("T")


def child_nodes(node: Node) -> list[Node]:
    """
    Structural children of a node, in field order.

    Reflects over the dataclass fields rather than a per-class table, so new
    Node types are traversable without touching this.
    """
    kids: list[Node] = []
    for f in dataclasses.fields(node):
        value = getattr(node, f.name)
        if isinstance(value, Node):
            kids.append(value)
        elif isinstance(value, list):
            kids.extend(v for v in value if isinstance(v, Node))
    return kids


class NodeVisitor(Generic[T]):
    """
    Base for AST passes that produce a value of type T.

    Dispatches on node class name, like ast.NodeVisitor, instead of GoF
    double dispatch. Keeps accept() off the nodes so they stay plain
    dataclasses.
    """

    def visit(self, node: Node) -> T:
        method = getattr(self, "visit_" + type(node).__name__, self.generic_visit)
        result: T = method(node)
        return result

    def generic_visit(self, node: Node) -> T:
        raise NotImplementedError(
            f"{type(self).__name__} has no rule for {type(node).__name__}"
        )


class TreeWalker(NodeVisitor[None]):
    """A visitor that recurses into every child by default and returns nothing."""

    def generic_visit(self, node: Node) -> None:
        for child in child_nodes(node):
            self.visit(child)


class ColumnRefCollector(TreeWalker):
    """Collects every ColumnRef in a subtree, in source order."""

    def __init__(self) -> None:
        self.refs: list[ColumnRef] = []

    def visit_ColumnRef(self, node: ColumnRef) -> None:
        self.refs.append(node)


# ---------------------------------------------------------------------------
# Stage 3: Semantic validator  -  AST + schema  ->  resolved fields + errors
# ---------------------------------------------------------------------------

@dataclass
class ResolvedField:
    """One column of the generated bean: its name and the SQL type to map from."""
    field_name: str
    datatype: str
    source_table: Optional[str]


@dataclass
class ResolutionContext:
    """
    Alias map, tables in scope, and collected errors, bundled so they are
    threaded as one value instead of three arguments.
    """
    alias_to_table: dict[str, str]
    involved_tables: list[str]
    errors: list[str]


class TypeInferenceVisitor(NodeVisitor[str]):
    """
    Infers the SQL type an expression yields.

    Small rule table: this picks the bean field's type, not the exact type
    MySQL would return.
    """

    _FUNCTION_TYPES = {
        "COUNT": "int",
        "SUM": "decimal",
        "AVG": "decimal",
        "MIN": "inherit",   # takes the type of its argument column
        "MAX": "inherit",
    }

    def __init__(self, validator: "SemanticValidator", ctx: "ResolutionContext") -> None:
        self._validator = validator
        self._ctx = ctx

    def visit_Literal(self, node: Literal) -> str:
        if not node.is_number:
            return "varchar"
        return "decimal" if "." in node.value else "int"

    def visit_BinaryExpr(self, node: BinaryExpr) -> str:
        left = self.visit(node.left)
        right = self.visit(node.right)
        return "decimal" if "decimal" in (left, right) else "int"

    def visit_FunctionCall(self, node: FunctionCall) -> str:
        rule = self._FUNCTION_TYPES.get(node.name.upper(), "varchar")
        if rule != "inherit":
            return rule
        # MIN/MAX inherit the type of their first column argument, if any.
        for arg in node.args:
            if isinstance(arg, ColumnRef):
                resolved = self._validator.resolve_column(arg, self._ctx)
                if resolved is not None:
                    return resolved[1].datatype
        return "varchar"

    def visit_ColumnRef(self, node: ColumnRef) -> str:
        resolved = self._validator.resolve_column(node, self._ctx)
        return resolved[1].datatype if resolved else "varchar"

    def generic_visit(self, node: Node) -> str:
        # Star, and anything else without a meaningful scalar type.
        return "varchar"


class SemanticValidator:
    """
    Checks an AST against a schema: resolves table aliases, confirms every
    referenced column exists, flags columns ambiguous across joined tables,
    and produces the typed field list for the code generator.
    """

    def __init__(self, schema: SchemaProvider):
        self.schema = schema

    def validate(self, stmt: SelectStatement) -> tuple[list[ResolvedField], list[str]]:
        ctx = ResolutionContext(alias_to_table={}, involved_tables=[], errors=[])

        # 1. Resolve which real tables are in play and build the alias map.
        for ref in stmt.from_tables + [j.table for j in stmt.joins]:
            table = self.schema.get_table(ref.name)
            if table is None:
                ctx.errors.append("Unknown table '" + ref.name + "'")
                continue
            ctx.involved_tables.append(ref.name)
            ctx.alias_to_table[ref.name.lower()] = ref.name
            if ref.alias:
                ctx.alias_to_table[ref.alias.lower()] = ref.name

        # 2. Validate every column reference anywhere in the statement.
        collector = ColumnRefCollector()
        collector.visit(stmt)
        for ref in collector.refs:
            self.resolve_column(ref, ctx)

        # 3. Build the bean's field list from the SELECT clause only.
        fields = self._resolve_select_fields(stmt, ctx)

        # A column referenced in more than one clause can raise the same error
        # twice; report each distinct problem once, in first-seen order.
        deduped: list[str] = []
        for msg in ctx.errors:
            if msg not in deduped:
                deduped.append(msg)
        return fields, deduped

    # -- helpers -------------------------------------------------------------

    def resolve_column(
        self, ref: ColumnRef, ctx: ResolutionContext
    ) -> Optional[tuple[str, ColumnSchema]]:
        """Return (table_name, ColumnSchema) for a reference, or record an error."""
        if ref.qualifier is not None:
            real = ctx.alias_to_table.get(ref.qualifier.lower())
            if real is None:
                ctx.errors.append("Unknown table alias '" + ref.qualifier + "'")
                return None
            table = self.schema.get_table(real)
            col = table.get_column(ref.column) if table else None
            if col is None:
                ctx.errors.append(
                    "Column '" + ref.column + "' not found in table '" + real + "'"
                )
                return None
            return real, col

        # Unqualified: search every involved table, reject if more than one matches.
        matches: list[tuple[str, ColumnSchema]] = []
        for table_name in ctx.involved_tables:
            table = self.schema.get_table(table_name)
            col = table.get_column(ref.column) if table else None
            if col is not None:
                matches.append((table_name, col))

        if not matches:
            ctx.errors.append(
                "Column '" + ref.column + "' not found in any referenced table"
            )
            return None
        if len(matches) > 1:
            where = ", ".join(t for t, _ in matches)
            ctx.errors.append(
                "Column '" + ref.column + "' is ambiguous (exists in " + where + ")"
            )
            return None
        return matches[0]

    def _resolve_select_fields(
        self, stmt: SelectStatement, ctx: ResolutionContext
    ) -> list[ResolvedField]:
        fields: list[ResolvedField] = []
        used_names: set[str] = set()
        auto_index = 0
        inferencer = TypeInferenceVisitor(self, ctx)

        for item in stmt.columns:
            expr = item.expr

            if isinstance(expr, Star):
                for table_name in ctx.involved_tables:
                    table = self.schema.get_table(table_name)
                    if not table:
                        continue
                    for col in table.columns:
                        name = self._unique(col.name, table_name, used_names)
                        fields.append(ResolvedField(name, col.datatype, table_name))
                continue

            if isinstance(expr, ColumnRef):
                resolved = self.resolve_column(expr, ctx)
                if resolved is None:
                    continue
                table_name, col = resolved
                name = item.alias or col.name
                name = self._unique(name, table_name, used_names)
                fields.append(ResolvedField(name, col.datatype, table_name))
                continue

            # Function call or arithmetic expression: needs a name and a type.
            datatype = inferencer.visit(expr)
            if item.alias:
                name = item.alias
            else:
                auto_index += 1
                name = "column" + str(auto_index)
            name = self._unique(name, None, used_names)
            fields.append(ResolvedField(name, datatype, None))

        return fields

    @staticmethod
    def _unique(name: str, table_name: Optional[str], used: set[str]) -> str:
        # Avoid two bean fields with the same name (e.g. 'id' from two tables).
        candidate = name
        if candidate.lower() in used and table_name:
            candidate = table_name + "_" + name
        base = candidate
        n = 2
        while candidate.lower() in used:
            candidate = base + "_" + str(n)
            n += 1
        used.add(candidate.lower())
        return candidate


# ---------------------------------------------------------------------------
# Stage 4: Code generation  -  resolved fields  ->  Java source
# ---------------------------------------------------------------------------

class UnmappedTypeError(Exception):
    pass


class TypeMapper:
    """Maps SQL data types to Java types. Fails loudly on an unknown type."""

    _MAP = {
        "varchar": "String", "char": "String", "text": "String",
        "int": "int", "integer": "int", "tinyint": "int", "smallint": "int",
        "mediumint": "int", "bigint": "long",
        "decimal": "BigDecimal", "numeric": "BigDecimal",
        "float": "float", "double": "double",
        "date": "LocalDate", "datetime": "LocalDateTime",
        "timestamp": "LocalDateTime", "time": "LocalTime",
        "boolean": "boolean", "bool": "boolean", "bit": "boolean",
    }

    def to_java(self, sql_type: str) -> str:
        java = self._MAP.get(sql_type.lower())
        if java is None:
            raise UnmappedTypeError(
                f"No Java mapping for SQL type '{sql_type}'. Add it to TypeMapper._MAP."
            )
        return java


class CodeGenerator:
    """Emits a Java bean (POJO) from a list of resolved fields."""

    _INDENT = "    "

    def __init__(self, type_mapper: Optional[TypeMapper] = None):
        self.type_mapper = type_mapper or TypeMapper()

    def generate(self, class_name: str, fields: list[ResolvedField]) -> str:
        java_fields = [(f.field_name, self.type_mapper.to_java(f.datatype)) for f in fields]

        lines: list[str] = [f"public class {class_name} {{", ""]

        for name, jtype in java_fields:
            lines.append(f"{self._INDENT}private {jtype} {name};")
        lines.append("")

        # no-argument constructor
        lines.append(f"{self._INDENT}public {class_name}() {{")
        lines.append(f"{self._INDENT}}}")
        lines.append("")

        # all-arguments constructor
        params = ", ".join(f"{jtype} {name}" for name, jtype in java_fields)
        lines.append(f"{self._INDENT}public {class_name}({params}) {{")
        for name, _ in java_fields:
            lines.append(f"{self._INDENT * 2}this.{name} = {name};")
        lines.append(f"{self._INDENT}}}")
        lines.append("")

        # getters and setters
        for name, jtype in java_fields:
            cap = name[0].upper() + name[1:]
            lines.append(f"{self._INDENT}public {jtype} get{cap}() {{")
            lines.append(f"{self._INDENT * 2}return {name};")
            lines.append(f"{self._INDENT}}}")
            lines.append("")
            lines.append(f"{self._INDENT}public void set{cap}({jtype} {name}) {{")
            lines.append(f"{self._INDENT * 2}this.{name} = {name};")
            lines.append(f"{self._INDENT}}}")
            lines.append("")

        lines.append("}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

class SemanticError(ValueError):
    """
    Raised when SQL parses but does not match the schema.

    Subclasses ValueError for backwards compatibility; `errors` holds the
    individual problems so callers need not parse the message.
    """

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__(
            "Semantic validation failed:\n  - " + "\n  - ".join(errors)
        )


def analyze(sql: str, schema: SchemaProvider, class_name: str = "QueryResult") -> str:
    """Run the full pipeline and return the generated Java bean.

    Raises LexError or ParseError on malformed SQL, SemanticError listing the
    problems if any column or table does not check out, and UnmappedTypeError
    if a resolved column has no Java equivalent.
    """
    tokens = Lexer(sql).tokenize()
    ast = Parser(tokens).parse()
    fields, errors = SemanticValidator(schema).validate(ast)
    if errors:
        raise SemanticError(errors)
    return CodeGenerator().generate(class_name, fields)


# ---------------------------------------------------------------------------
# A sample schema, so the pipeline can be exercised with no database at all.
# ---------------------------------------------------------------------------

def demo_schema() -> InMemorySchemaProvider:
    """A small two-table schema used by the demo and by the test suite."""
    bookings = TableSchema("bookings", [
        ColumnSchema("id", "int", None, None, False),
        ColumnSchema("passenger_id", "int", None, None, False),
        ColumnSchema("flight_id", "int", None, None, False),
        ColumnSchema("seat", "varchar", 8, None, True),
        ColumnSchema("price", "decimal", 10, 2, False),
    ])
    flights = TableSchema("flights", [
        ColumnSchema("id", "int", None, None, False),
        ColumnSchema("origin", "varchar", 64, None, False),
        ColumnSchema("destination", "varchar", 64, None, False),
        ColumnSchema("departs_at", "datetime", None, None, False),
    ])
    return InMemorySchemaProvider([bookings, flights])


if __name__ == "__main__":  # pragma: no cover
    # The command-line interface lives in cli.py; this keeps `python
    # sql_analyzer.py` working as a quick smoke check.
    from cli import main

    raise SystemExit(main(["--demo"]))
