"""Prompt-injection suite results as a table (milestone 5 README screenshot).

    .venv/bin/python -m scripts.injection_report

Runs every attack from tests/test_prompt_injection.py through the SQL guard as Aurora's analyst
(no database or model needed) and shows what happened to each: refused (and why), confined to
Aurora's rows, or a 403. Saves loadtest/results/injection.svg. Exits non-zero if any attack
got through.
"""
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table
from rich.text import Text

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
from test_prompt_injection import ATTACKS, AURORA, BRAND_ATTACKS, VIEWS  # noqa: E402

from chatbot import access, guard  # noqa: E402

OUT = ROOT / "loadtest" / "results" / "injection.svg"
GROUPS = {
    "extra statements": ["ddl after a select", "statement hidden behind a comment"],
    "writes, DDL, server commands": ["update", "insert select", "select into", "grant", "set role", "copy to file",
                                     "redshift unload to s3", "call procedure", "explain", "values"],
    "other tables and catalogs": ["fact table", "unrouted chat view", "quoted identifiers", "uppercase names",
                                  "pg_catalog passwords", "pg_user unqualified", "information_schema",
                                  "redshift svv", "redshift query log"],
    "smuggled inside a valid query": ["cte named like the forbidden table", "cte plus a second table",
                                      "scalar subquery", "exists subquery", "union", "lateral"],
    "dangerous or revealing functions": ["pg_read_file", "pg_sleep (tie up the database)",
                                         "query_to_xml (runs arbitrary SQL)", "set_config", "lo_import",
                                         "privilege probing", "current_user", "server version", "database name",
                                         "table function", "dblink"],
}


def outcome(sql: str) -> tuple[str, str]:
    try:
        out = guard.validate(sql, VIEWS, AURORA)
    except guard.UnsafeSQL as e:
        return "refused", str(e).split(";")[0]
    except access.Forbidden as e:
        return "403", str(e)
    if "brand IN ('Aurora Apparel')" in out:
        return "confined", "rows limited to Aurora Apparel"
    return "GOT THROUGH", out[:80]


def main() -> int:
    table = Table(title="Prompt-injection suite: SQL a hijacked model might write, run through the guard",
                  title_style="bold", header_style="bold cyan",
                  caption="as aurora.analyst; the guard -- not the model -- is the boundary · "
                          "tests/test_prompt_injection.py")
    for col in ("attack type", "attacks", "outcome", "example (first in group)"):
        table.add_column(col)
    leaked, total = 0, 0
    for group, names in GROUPS.items():
        results = [outcome(ATTACKS[n]) for n in names]
        total += len(results)
        bad = [r for r in results if r[0] == "GOT THROUGH"]
        leaked += len(bad)
        verdict = Text(f"{len(results) - len(bad)}/{len(results)} refused", style="green" if not bad else "bold red")
        table.add_row(group, str(len(names)), verdict, f"{names[0]}: {results[0][1]}")
    brand = [(n, outcome(sql)) for n, (sql, _) in BRAND_ATTACKS.items()]
    total += len(brand)
    held = [b for b in brand if b[1][0] in ("confined", "403")]
    leaked += len(brand) - len(held)
    table.add_row("another brand's rows", str(len(brand)),
                  Text(f"{sum(b[1][0] == 'confined' for b in brand)} confined, {sum(b[1][0] == '403' for b in brand)} "
                       f"x 403", style="green" if len(held) == len(brand) else "bold red"),
                  "or 1=1, no filter, LIKE, ||, <>, LOWER(), padding, CTE")
    table.add_section()
    table.add_row(Text("total", style="bold"), Text(str(total), style="bold"),
                  Text(f"{total - leaked}/{total} held", style="bold green" if not leaked else "bold red"), "")
    console = Console(record=True, width=140)
    console.print(table)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    console.save_svg(str(OUT), title="prompt-injection suite")
    return 0 if not leaked else 1


if __name__ == "__main__":
    sys.exit(main())
