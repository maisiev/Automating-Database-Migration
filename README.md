# SQL query migration helper

Create reviewable SQL Server query drafts from a local Excel or CSV column
mapping. This utility reads files only: it never connects to a database or runs
SQL. Original queries and mappings stay unchanged. Python 3.9+ is required.

The direction is fixed: `OCColumn` is the old name and `MSColumn` is the new name.
SQL Server parsing (`tsql`) is built in. No server, credentials, direction, or
dialect needs to be entered. Report definition JSON is outside the current scope.

## Install

Open a terminal in the folder containing `migrate_queries.py`.

Windows PowerShell:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest -v
```

macOS/Linux:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest -v
```

## Convert one downloaded query

Keep your UTF-8 query and mapping under `private/`, or outside the repository.
These inputs are not included with the utility. Preview the review log:

```powershell
.\.venv\Scripts\python.exe migrate_queries.py private\queries\query.sql --mapping private\mapping.xlsx --ignore-case --dry-run
```

Create a draft and log in a new output folder:

```powershell
.\.venv\Scripts\python.exe migrate_queries.py private\queries\query.sql --mapping private\mapping.xlsx --ignore-case --output outputs\run-001
```

Use `.venv/bin/python` and forward slashes on macOS/Linux. Add
`--table-map private\table-map.json` when you need local table/alias or join-key
configuration. `table-map.example` contains synthetic examples only: copy and
adapt it locally, and verify it against your own schema.

The output folder must be new and outside the input folder. Each run creates
`queries/` with draft SQL and `review.json` listing changes and review issues.
Line/column positions in the log refer to the original input. A directory input
is also supported, but one file at a time is sufficient. Cross-table mappings
require one SELECT per file. Export UTF-16 or NUL-containing inputs as clean UTF-8.

## Mapping format

The first row must contain `MSColumn`, `OCTable`, and `OCColumn`. For example:

| MSColumn | OCTable | OCColumn |
| --- | --- | --- |
| RecordId | legacy_records | legacy_id |

This synthetic row converts `legacy_records.legacy_id` to `RecordId`.
`OCTable` supplies context for names repeated across tables. Optional columns:

| Column | Purpose |
| --- | --- |
| MSTable | Destination table for a cross-table field move |
| MSAlias | Destination alias; required together with MSTable |
| ReviewRequired | `true`, `yes`, or `1` marks a provisional mapping |
| ReviewNote | Text to include in the review log |

Other columns, including `UpdateChanges`, are ignored. Use `--sheet NAME` if the
Excel file contains multiple sheets. `--ignore-case` opts into case-insensitive
matching. Blank identifiers, sentinel values, expressions, missing targets, and
conflicting mappings are flagged; identical duplicates are accepted.

Table configuration supports mapping contexts, destination tables, existing
alias renaming, and explicit join-key overrides. Schema/database qualifiers are
preserved. Cross-schema changes and real join relationships require review.
The utility includes a provisional Store/Site join workflow; confirm its keys,
tenant handling, join type, and row counts before relying on it. Local mappings
and configuration are the source of schema-specific decisions.

## How to read a draft

Every saved draft begins with a short review guide. Review items stay visible
in SQL comments and in the log:

- Renamed selected columns retain their old output labels through `AS old_name`.
- Flagged outputs become `NULL AS [output_name]`, with the original expression
  retained in a `REVIEW` comment. This includes provisional Site outputs.
- Flagged WHERE predicates become `(1 = 1)` with their original text commented
  out. Known AND-connected filters remain active. Affected OR groups are disabled
  as a whole, so results may include additional rows.
- Joins with unresolved tables or ON references are commented out in full.
  Dependent joins and their selected fields/filters are disabled too.
- The configured legacy report placeholders are commented out and flagged;
  other placeholders remain unchanged for review.

These are temporary testing changes, not verified replacements. Do not ignore
review items: review them with someone who understands the database structure.
Validate filters, joins, tenant constraints, permissions, and result counts in
an approved test environment before relying on the query or its results.

## Limitations and exit codes

Simple SELECTs, qualified joins, filters, aggregates, and explicit output aliases
are supported. Unqualified columns in joins are flagged instead of guessed.
Wildcards need schema review. CTEs, subqueries, set operations, DML/DDL, SELECT
INTO, USING/NATURAL joins, pivots, lateral/unnest, stored procedures, dynamic SQL,
and batch separators need manual handling.

If a disabled join's alias appears in grouping, ordering, HAVING, windows, or a
qualified wildcard, that file stops for manual review instead of producing
dangling references. Parse errors also prevent a draft for that file. Successful
parsing does not prove a query will run or return correct results.

- `0`: drafts have no query-level review flags.
- `1`: at least one query has review flags or a per-file error.
- `2`: setup/input error, such as an invalid mapping or existing output folder.

Workbook-wide mapping issues are logged but do not alone cause exit code 1.
The generated `review.json` is a conversion log, not a report definition.
Report definition JSON is not read, validated, copied, or modified.

## Private inputs

The repository ignores workbooks, CSV/SQL/JSON files, generated archives, virtual
environments, credentials, `private/`, `inputs/`, and `outputs/`. Ignore rules do
not remove files already tracked by Git. Keep all real queries, mappings,
schema configuration, report definitions, outputs, and work notes local.

Only utility code, synthetic tests/examples, and generic documentation belong in
the repository. Inspect staged filenames and content before committing or pushing.
