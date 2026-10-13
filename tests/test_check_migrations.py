"""Migration-safety check (scripts/check_migrations.py): the rules, the allow comment, history."""
from pathlib import Path

import pytest

from scripts.check_migrations import check_history, check_sql

BASE = [p.name for p in sorted((Path(__file__).resolve().parent.parent / "sql" / "redshift").glob("V*.sql"))]
NEXT = f"V{len(BASE) + 1:03d}__next.sql"


def rules(sql: str) -> list[str]:
    return [f.rule for f in check_sql(NEXT, sql)]


@pytest.mark.parametrize("sql,rule", [
    ("DROP TABLE fact.fact_sales;", "drop-table"),
    ("drop schema stage cascade;", "drop-table"),
    ("ALTER TABLE etl.load_audit DROP COLUMN triage_note;", "drop-column"),
    ("ALTER TABLE etl.load_audit DROP triage_note;", "drop-column"),
    ("TRUNCATE fact.fact_sales;", "truncate"),
    ("DELETE FROM etl.load_audit;", "unbounded-dml"),
    ("UPDATE etl.load_audit SET status = 'X';", "unbounded-dml"),
    ("ALTER TABLE dim.client RENAME TO dim.brand;", "rename"),
    ("ALTER TABLE dim.client RENAME COLUMN client_name TO brand_name;", "rename"),
    ("ALTER TABLE dim.client ALTER COLUMN client_name TYPE VARCHAR(400);", "alter-type"),
    ("ALTER TABLE dim.client ADD COLUMN tier VARCHAR(10) NOT NULL;", "not-null-no-default"),
    ("DROP VIEW chat.v_calendar;", "drop-view"),
])
def test_risky_statements_are_flagged(sql, rule):
    assert rules(sql) == [rule]


@pytest.mark.parametrize("sql", [
    "CREATE TABLE etl.x (id INT);",
    "ALTER TABLE etl.load_audit ADD COLUMN triage_cost DECIMAL(10,4);",
    "ALTER TABLE dim.client ADD COLUMN tier VARCHAR(10) NOT NULL DEFAULT 'std';",
    "ALTER TABLE dim.client DROP CONSTRAINT client_pk;",
    "DELETE FROM etl.load_audit WHERE status = 'TEST';",
    "UPDATE etl.load_audit SET status = 'X' WHERE load_id = 1;",
    "CREATE OR REPLACE VIEW chat.v_calendar AS SELECT 1;",
    "DROP VIEW chat.v_calendar;\nCREATE VIEW chat.v_calendar AS SELECT 1;",      # re-created in the same file
    "-- DROP TABLE fact.fact_sales;  (only a comment)\nSELECT 1;",
    "INSERT INTO etl.notes VALUES ('please DROP TABLE x; TRUNCATE y');",            # only a string
])
def test_safe_statements_pass(sql):
    assert rules(sql) == []


def test_allow_comment_with_a_reason_lets_one_rule_through():
    sql = ("-- migration-safety: allow drop-column: triage_tokens unused since V011, nothing reads it\n"
           "ALTER TABLE etl.load_audit DROP COLUMN triage_tokens;\nTRUNCATE etl.tmp;")
    assert rules(sql) == ["truncate"]


def test_allow_comment_without_a_reason_does_not_count():
    assert rules("-- migration-safety: allow truncate:\nTRUNCATE etl.tmp;") == ["truncate"]


def test_findings_point_at_the_statement_line():
    (finding,) = check_sql(NEXT, "-- header\nCREATE TABLE etl.a (id INT);\n\n-- why\nTRUNCATE etl.a;\n")
    assert finding.line == 5 and finding.path == NEXT


def test_every_existing_migration_passes_the_content_rules():
    root = Path(__file__).resolve().parent.parent / "sql" / "redshift"
    assert [f for name in BASE for f in check_sql(name, (root / name).read_text())] == []


def test_editing_deleting_or_renaming_an_applied_migration_fails():
    changes = [("M", f"sql/redshift/{BASE[0]}"), ("D", f"sql/redshift/{BASE[1]}"),
               ("R", f"sql/redshift/{BASE[2]}"), ("A", f"sql/redshift/{NEXT}")]
    findings = check_history(BASE, changes, sorted(BASE[3:] + [NEXT]))
    assert [(f.rule, f.message.split(" was ")[1].split(":")[0]) for f in findings] == [
        ("history", "edited"), ("history", "deleted"), ("history", "renamed")]


def test_new_migration_must_come_after_the_base_branch():
    late = "V005__sneaky.sql"   # V005 is taken; even a free low number would apply out of order
    findings = check_history(BASE, [("A", f"sql/redshift/{late}")], sorted(BASE + [late]))
    rules_hit = [f.message for f in findings]
    assert any("also" in m for m in rules_hit) and any("numbered after" in m for m in rules_hit)


def test_bad_filename_fails():
    findings = check_history(BASE, [("A", "sql/redshift/add_index.sql")], sorted(BASE + ["add_index.sql"]))
    assert [f.rule for f in findings] == ["naming"]


def test_a_clean_new_migration_passes():
    assert check_history(BASE, [("A", f"sql/redshift/{NEXT}")], sorted(BASE + [NEXT])) == []
