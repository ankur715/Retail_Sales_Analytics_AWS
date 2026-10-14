"""Validate LLM-written SQL before it reaches Redshift.

Parsed with sqlglot (not keyword matching -- a column named updated_at must
not be rejected for containing "update"). Accepted only if it is:
  - exactly one statement, and a read-only query (SELECT / UNION / WITH)
  - reading only the views this question was routed to (chat.* allowlist);
    CTE names defined in the query itself are allowed
  - only known functions (milestone 5): anything sqlglot doesn't recognise as a standard
    function is refused (pg_read_file, pg_sleep, set_config, query_to_xml, lo_import, ...),
    as are functions that reveal who/where the query runs (current_user, version(), ...)
  - row-capped: a missing or larger LIMIT becomes MAX_ROWS
  - confined to the caller's brands (milestone 4): every view with a brand column is
    replaced by a subquery filtered to those brands, and SQL that names another brand is
    refused (access.Forbidden -> 403). This doesn't depend on what the model wrote.

The database user is read-only on the chat schema anyway; this is the
second line of defence and gives Gemini a precise error to fix.
"""
import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from chatbot import access

MAX_ROWS = 500
WRITE_NODES = (exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop, exp.Alter,
               exp.Command, exp.Grant, exp.TruncateTable, exp.Copy)


# Recognised by sqlglot but reveal the session or server: not needed to answer a sales question
ENVIRONMENT_FUNCS = (exp.CurrentUser, exp.SessionUser, exp.CurrentVersion, exp.CurrentDatabase, exp.CurrentSchema)


class UnsafeSQL(ValueError):
    pass


def validate(sql: str, allowed_views: list[str], brands: frozenset[str] | None = None) -> str:
    """Return the cleaned, row-capped (and, with `brands`, brand-confined) SQL, or raise
    UnsafeSQL with the reason / access.Forbidden for another brand's data."""
    sql = sql.strip().strip("`").removeprefix("sql").strip().rstrip(";")
    try:
        statements = [s for s in sqlglot.parse(sql, read="redshift") if s is not None]
    except ParseError as e:
        raise UnsafeSQL(f"SQL does not parse: {e}") from e
    if len(statements) != 1:
        raise UnsafeSQL(f"expected exactly one statement, got {len(statements)}")
    tree = statements[0]

    if not isinstance(tree, (exp.Select, exp.Union)):
        raise UnsafeSQL(f"only SELECT queries are allowed, got {type(tree).__name__}")
    if any(isinstance(node, WRITE_NODES) for node in tree.walk()):
        raise UnsafeSQL("query contains a write or DDL operation")

    for func in tree.find_all(exp.Func):
        if isinstance(func, exp.Anonymous):        # not a function sqlglot knows: refuse, don't guess
            raise UnsafeSQL(f"function {func.name.upper()}() is not allowed; use standard SQL functions")
        if isinstance(func, ENVIRONMENT_FUNCS):
            raise UnsafeSQL(f"{func.sql(dialect='redshift')} is not allowed")

    allowed = {v.lower() for v in allowed_views}
    cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    for table in tree.find_all(exp.Table):
        if not table.name:                          # FROM generate_series(...), dblink(...)
            raise UnsafeSQL("table functions are not allowed; select from the views")
        name = f"{table.db}.{table.name}".lower() if table.db else table.name.lower()
        if not table.db and name in cte_names:
            continue
        if name not in allowed:
            raise UnsafeSQL(f"table {name} is not allowed; use only: {', '.join(sorted(allowed))}")

    limit = tree.args.get("limit")
    current = limit.expression if limit else None
    if current is None or not (current.is_int and int(current.name) <= MAX_ROWS):
        tree.set("limit", exp.Limit(expression=exp.Literal.number(MAX_ROWS)))
    if brands is not None:
        _confine(tree, brands)
    return tree.sql(dialect="redshift")


def _confine(tree: exp.Expression, brands: frozenset[str]) -> None:
    # 1. A clear refusal when the query names a brand the caller can't see
    known = {b.lower(): b for b in access.client_brands().values()}
    for literal in tree.find_all(exp.Literal):
        if literal.is_string:
            brand = known.get(literal.name.strip().lower())
            if brand and brand not in brands:
                raise access.Forbidden(f"You don't have access to {brand}'s data")
    # 2. The enforcement: each brand view -> (SELECT * FROM view WHERE brand IN (...)) AS same name
    col, confined = access.brand_column(), access.brand_views()
    allowed = [exp.Literal.string(b) for b in sorted(brands)] or [exp.Null()]   # no brands: no rows
    for table in list(tree.find_all(exp.Table)):
        name = f"{table.db}.{table.name}".lower()
        if not table.db or name not in confined:
            continue
        inner = exp.select("*").from_(exp.Table(this=exp.to_identifier(table.name), db=exp.to_identifier(table.db)))
        inner = inner.where(exp.In(this=exp.column(col), expressions=allowed))
        table.replace(exp.Subquery(this=inner, alias=exp.TableAlias(this=exp.to_identifier(table.alias_or_name))))
