# SQL Query Analyzer

Parses a SQL `SELECT` statement, validates it against a real database schema,
and generates the matching Java bean.

It is a small compiler front-end rather than a string-manipulation script: the
query is tokenized, parsed into an AST by a hand-written recursive-descent
parser, checked against live `INFORMATION_SCHEMA` metadata, and only then used
to emit code.

```
raw SQL text
  -> Lexer              a stream of typed Tokens
  -> Parser             an Abstract Syntax Tree
  -> SemanticValidator  alias resolution + schema checking (visitor passes)
  -> CodeGenerator      a Java bean for the SELECT list
```

## Example

```console
$ python cli.py --demo
```

```sql
SELECT b.seat AS seatNumber, b.price, f.destination, COUNT(b.id) AS total
FROM bookings b
JOIN flights f ON b.flight_id = f.id
WHERE b.price + 10 > 100
GROUP BY f.destination
```

```java
public class QueryResult {

    private String seatNumber;
    private BigDecimal price;
    private String destination;
    private int total;

    public QueryResult() {
    }

    public QueryResult(String seatNumber, BigDecimal price, String destination, int total) {
        this.seatNumber = seatNumber;
        this.price = price;
        this.destination = destination;
        this.total = total;
    }

    public String getSeatNumber() {
        return seatNumber;
    }

    public void setSeatNumber(String seatNumber) {
        this.seatNumber = seatNumber;
    }

    // ... remaining accessors
}
```

## What it catches

Because validation runs against the real schema, mistakes are caught before the
query ever reaches the database:

| Query | Reported |
| --- | --- |
| `SELECT FROM bookings` | `ParseError: expected an expression ... at position 7` |
| `SELECT b.nope FROM bookings b` | `Column 'nope' not found in table 'bookings'` |
| `SELECT z.seat FROM bookings b` | `Unknown table alias 'z'` |
| `SELECT id FROM bookings b JOIN flights f ON b.flight_id = f.id` | `Column 'id' is ambiguous (exists in bookings, flights)` |

Types are resolved too: `COUNT(...)` becomes `int`, `SUM`/`AVG` become
`BigDecimal`, `MIN`/`MAX` inherit their argument's type, and `price + 10` is
inferred as `decimal` because decimal is contagious across arithmetic.

## Usage

```console
$ python cli.py --demo                                   # built-in schema, sample query
$ python cli.py --demo --sql "SELECT seat FROM bookings" # built-in schema, your query
$ python cli.py --sql "SELECT ..." -c Booking -o Booking.java
$ cat query.sql | python cli.py -f - -c Booking   # '-' means stdin
```

Against a live database, connection settings come from the environment, never
from the source:

```console
$ export MYSQL_PASSWORD=...      # required
$ export MYSQL_DATABASE=flights  # required
$ export MYSQL_HOST=localhost    # optional, defaults shown
$ export MYSQL_USER=root
$ python cli.py -f query.sql -c Booking
```

Exit codes: `0` success, `1` usage, `2` syntax error, `3` semantic error,
`4` unmapped SQL type.

## Design

**The database sits behind an interface.** `SemanticValidator` depends on the
abstract `SchemaProvider`, not on MySQL. `MySQLSchemaProvider` is the real
implementation; `InMemorySchemaProvider` supplies the same metadata from plain
Python objects. That inversion is why all 121 tests run with no database, no
network, and no fixtures to tear down.

**Tree passes are visitors.** Collecting column references and inferring
expression types are `NodeVisitor` subclasses rather than `isinstance` chains
inside the validator, so a new analysis is a new class. Dispatch is on node
class name (like `ast.NodeVisitor`) instead of GoF double dispatch, which keeps
AST nodes as plain dataclasses. `child_nodes()` reflects over dataclass fields,
so a new node type is traversable without touching the traversal code.

**Precedence is structural.** The expression grammar is one method per
precedence level (`OR`, `AND`, comparison, additive, multiplicative, primary),
so `a + b * c` and `a OR b AND c` group correctly by construction.

**Failures are loud.** An unmapped SQL type raises `UnmappedTypeError` instead
of quietly emitting `Object`, and `SemanticError` carries the individual
problems as a list rather than forcing callers to parse a message.

## Development

```console
$ pip install -r requirements-dev.txt
$ pytest          # no database required
$ mypy            # clean
```

CI runs both on Python 3.10 through 3.13.

## Scope

Deliberately limited to a single `SELECT`: no subqueries, derived tables,
`UNION`, or CTEs. `WHERE` / `GROUP BY` / `ORDER BY` are parsed and their columns
validated, but only the `SELECT` list shapes the generated bean. Expression
return types use a small documented inference table rather than MySQL's full
type-promotion rules.
