"""Migration-safety check (milestone 2): fail CI before a risky Redshift migration merges.

Compares sql/redshift/ with the base branch and checks:

  history   applied migrations are never edited, deleted or renamed (etl.migrate would refuse
            them anyway, but only at deploy time); a new file is named V<NNN>__<name>.sql and
            numbered after everything already on the base branch.
  content   statements in NEW migrations that lose data or break readers (the chat views, the
            DAGs, the chatbot) while the old code is still running:
              drop-table      DROP TABLE / DROP SCHEMA
              drop-column     ALTER TABLE ... DROP COLUMN
              drop-view       DROP VIEW that the same file doesn't re-create
              truncate        TRUNCATE
              unbounded-dml   DELETE or UPDATE without WHERE
              rename          ALTER TABLE ... RENAME (table or column): readers break at once
              alter-type      ALTER COLUMN ... TYPE (Redshift rewrites or refuses it)
              not-null-no-default  ADD COLUMN ... NOT NULL without DEFAULT (fails on a non-empty table)

A deliberate exception is allowed with a comment in the migration that names the rule and
gives a reason, so the reviewer sees it in the diff:

    -- migration-safety: allow drop-column: column unused since V009, readers moved to v_sales_by_week

Usage:  python -m scripts.check_migrations [--base origin/dev]
"""
import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from etl.migrate import FILENAME, split_statements

ROOT = Path(__file__).resolve().parent.parent
SQL_DIR = "sql/redshift"
ALLOW = re.compile(r"--[ \t]*migration-safety:[ \t]*allow[ \t]+([\w-]+)[ \t]*:[ \t]*(\S.*)", re.IGNORECASE)

# (rule, pattern on one comment-free, whitespace-collapsed, upper-cased statement)
RULES = [
    ("drop-table", re.compile(r"^DROP (TABLE|SCHEMA)\b")),
    ("drop-column", re.compile(r"^ALTER TABLE .* DROP (COLUMN )?(?!CONSTRAINT\b|DEFAULT\b|NOT NULL\b)\w")),
    ("truncate", re.compile(r"^TRUNCATE\b")),
    ("unbounded-dml", re.compile(r"^(DELETE FROM|UPDATE) (?!.*\bWHERE\b)")),
    ("rename", re.compile(r"^ALTER TABLE .* RENAME\b")),
    ("alter-type", re.compile(r"^ALTER TABLE .* ALTER (COLUMN )?\S+ TYPE\b")),
    ("not-null-no-default", re.compile(r"^ALTER TABLE .* ADD (COLUMN )?(?!.*\bDEFAULT\b).*\bNOT NULL\b")),
]
DROP_VIEW = re.compile(r"^DROP VIEW (IF EXISTS )?([\w.]+)")
CREATE_VIEW = re.compile(r"^CREATE (OR REPLACE )?VIEW ([\w.]+)")


@dataclass
class Finding:
    path: str
    line: int
    rule: str
    message: str

    def __str__(self):
        if os.environ.get("GITHUB_ACTIONS"):   # an inline annotation on the PR diff
            return f"::error file={self.path},line={self.line},title=migration-safety {self.rule}::{self.message}"
        return f"{self.path}:{self.line}: [{self.rule}] {self.message}"


def _normalize(statement: str) -> str:
    """Upper-case, comments and string literals removed, whitespace collapsed."""
    lines = [line.split("--", 1)[0] for line in statement.splitlines()]
    text = re.sub(r"'(?:[^']|'')*'", "''", " ".join(lines))
    return " ".join(text.upper().split())


def check_sql(path: str, text: str) -> list[Finding]:
    """Risky statements in one new migration, minus the ones it explicitly allows."""
    allowed = {m.group(1).lower() for m in ALLOW.finditer(text)}
    statements, offset, findings = [], 0, []
    for statement in split_statements(text):
        at = text.find(statement, offset)
        offset = at + len(statement) if at >= 0 else offset
        line = text.count("\n", 0, at) + 1 if at >= 0 else 1
        # skip leading comment lines so the line number points at the SQL itself
        line += next((i for i, s in enumerate(statement.splitlines()) if s.strip() and not s.strip().startswith("--")), 0)
        statements.append((line, _normalize(statement)))
    created = {m.group(2) for _, s in statements if (m := CREATE_VIEW.match(s))}
    for line, sql in statements:
        hits = [rule for rule, pattern in RULES if pattern.search(sql)]
        if (m := DROP_VIEW.match(sql)) and m.group(2) not in created:
            hits.append("drop-view")
        for rule in hits:
            if rule not in allowed:
                findings.append(Finding(path, line, rule, f"{sql[:90]} -- unsafe while old code still runs; "
                                        f"split it into steps, or add '-- migration-safety: allow {rule}: <reason>'"))
    return findings


def check_history(base: list[str], changes: list[tuple[str, str]], current: list[str]) -> list[Finding]:
    """base: migration filenames on the base branch; changes: (git status letter, path) from
    `git diff --name-status`; current: filenames now."""
    findings = []
    for status, path in changes:
        name = Path(path).name
        if status[0] in "MDR" and name in base:
            verb = {"M": "edited", "D": "deleted", "R": "renamed"}[status[0]]
            findings.append(Finding(path, 1, "history", f"{name} is already applied and was {verb}: "
                                    "never change an applied migration, add a new one"))
    top = max((int(m.group(1)) for n in base if (m := FILENAME.match(n))), default=0)
    seen = {}
    for name in current:
        m = FILENAME.match(name)
        if not m:
            findings.append(Finding(f"{SQL_DIR}/{name}", 1, "naming", "migrations are named V<NNN>__<name>.sql"))
            continue
        version = int(m.group(1))
        if version in seen:
            findings.append(Finding(f"{SQL_DIR}/{name}", 1, "naming", f"version {version} is also {seen[version]}"))
        seen[version] = name
        if name not in base and version <= top:
            findings.append(Finding(f"{SQL_DIR}/{name}", 1, "naming",
                                    f"new migration must be numbered after V{top:03d} (the base branch's latest)"))
    return findings


def _git(*args) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default=os.environ.get("MIGRATION_BASE", "origin/dev"),
                        help="branch or commit to compare with (default origin/dev)")
    base_ref = parser.parse_args(argv).base
    merge_base = _git("merge-base", base_ref, "HEAD").strip()
    base = [Path(p).name for p in _git("ls-tree", "--name-only", f"{merge_base}:{SQL_DIR}").split()]
    # working tree vs the merge base: committed, staged and unstaged edits, plus untracked files
    changes = []
    for line in _git("diff", "--name-status", "-M", merge_base, "--", SQL_DIR).splitlines():
        status, *paths = line.split("\t")
        changes.append((status[0], paths[0]))
        if status[0] == "R":
            changes.append(("A", paths[1]))   # the new name is checked like any new file
    changes += [("A", p) for p in _git("ls-files", "--others", "--exclude-standard", SQL_DIR).split()]
    current = sorted(p.name for p in (ROOT / SQL_DIR).glob("*.sql"))

    findings = check_history(base, changes, current)
    for status, path in changes:
        if status == "A" and Path(path).name not in base and path.endswith(".sql"):
            findings += check_sql(path, (ROOT / path).read_text())
    new = [n for n in current if n not in base]
    for finding in findings:
        print(finding)
    print(f"migration-safety: {len(new)} new migration(s) vs {base_ref} "
          f"({', '.join(new) or 'none'}), {len(findings)} problem(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
