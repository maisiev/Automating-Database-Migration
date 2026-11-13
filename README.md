# Report Migration Workbench

A small offline project for producing reviewable SQL and report definitions from explicit mapping plans. It demonstrates reproducible builds, conservative handling of unknown fields, data-driven tests and publication checks.

All bundled schemas, report definitions and records are fictional. The project has no database credentials, network clients, organization-specific adapters or inherited repository history.

## What it does

- Reads a documented, vendor-neutral JSON report format and a declared target schema.
- Generates SQL Server or SQLite SELECT queries and matching JSON column definitions.
- Keeps missing mappings as typed `NULL` outputs with review reasons.
- Blocks rows when the source or access partition is unresolved.
- Runs declarative fixture cases in temporary, in-memory SQLite databases.
- Creates deterministic output bundles and detects edits before reusing them.
- Records input hashes without copying input files or arbitrary source archives.

It is not a universal SQL converter. Converting an existing reporting system into the input format, confirming field meanings and validating the real database remain separate tasks. This project does not migrate stored records or create production schema changes.

## Quick start

Use a supported Python 3 interpreter. There are no third-party dependencies. The implementation uses Python 3.9-compatible syntax; the accompanying verification record states the interpreter actually used for testing. Running from the checkout requires no installation.

```sh
python3 -B -m unittest discover -s tests -v
python3 -B -m report_workbench build --project examples/project.json
python3 -B -m report_workbench audit --root .
```

The build prints its bundle directory under `.artifacts/catalog-balance/`. That generated directory is ignored by Git. Run `verify` with the printed directory to check its hashes and reproduce it from the same inputs:

```sh
python3 -B -m report_workbench verify BUNDLE_DIRECTORY --project examples/project.json
```

Replace `BUNDLE_DIRECTORY` with the path printed by `build`. A repeat build with identical inputs returns `unchanged`. It will refuse to replace a bundle that someone edited or an earlier run left incomplete.

## Fictional example

The [example project](examples/project.json) contains:

| File | Purpose |
| --- | --- |
| [report.json](examples/report.json) | Original output names, labels, logical types and visibility. |
| [schema.json](examples/schema.json) | A declared products/aisles target schema. |
| [plan.json](examples/plan.json) | Explicit mappings, joins, access scope and calculations. |
| [fixtures.json](examples/fixtures.json) | Synthetic rows and expected outcomes. |

The example preserves seven columns. One historical note has no mapping, so it remains `NULL`. The fixtures check two isolated workspaces, a missing aisle, disabled products, zero quantities and unknown counts. They also check that a `NULL` scope selects nothing.

The primary query uses runtime parameters. The standalone SQL uses review defaults; the scope defaults to `NULL` until an operator supplies an intended value. No workspace is discovered or selected automatically.

## Add a report

1. Describe the source report in JSON using the same shape as the example report. Every source output needs an ID, label and logical type. This normalized format intentionally has no proprietary metadata tables or permission tokens.
2. Create an initial plan:

   ```sh
   python3 -B -m report_workbench init --report examples/report.json --output new-plan.json
   ```

   Every initial mapping is unknown. The initializer infers no target field, join or row source. Existing files are never overwritten.

3. Declare the real target schema in a separate JSON file. This is a declaration for offline checks, not evidence that those fields exist in a deployed database.
4. Fill in verified mapping expressions and evidence, or retain `expression: null` with a review reason. Mark the source reviewed only after checking row membership. Configure a scope column and a bound parameter whose default remains `NULL`.
5. Add fixture data with meaningful positive and negative cases. Point a new project file at the report, schema, plan and optional fixture file, using paths relative to that project file.
6. Build into an artifact directory, inspect the generated files and run appropriate database/service tests before treating the result as approved.

Only JSON configuration and fixture data vary by report. A new general expression capability requires a shared compiler change and regression tests, rather than a report-specific script.

## Structured expressions

Expressions are data, not executable SQL or Python:

```json
{"column": "p.sku"}
```

```json
{"parameter": "workspace"}
```

```json
{
  "op": "multiply",
  "args": [{"column": "p.available"}, {"column": "p.unit_price"}]
}
```

Supported forms are `column`, `parameter`, `literal`, `op`/`args`, and `case`/`else`. Operators include arithmetic, comparisons, `and`, `or`, `not`, `upper`, `lower`, `abs`, `round` and `coalesce`. Arbitrary SQL fragments and arbitrary function calls are rejected. Division uses a null denominator check: an undefined quotient stays `NULL` rather than assuming a denominator or returning zero.

Column references must be qualified as `alias.column` and exist in the declared schema. Output identifiers are validated and quoted. Literal strings are escaped, and runtime parameters remain bound placeholders. A mapped expression must have a compatible output type; narrowing a numeric result into integer metadata is rejected.

Logical types are `text`, `integer`, `number`, `boolean`, `date` and `datetime`. SQL Server uses corresponding standard scalar types; SQLite is the offline fixture backend. Production precision, date handling, collation and SQL Server compilation are not proven by SQLite.

The default scope is a column/parameter equality. A reviewed, genuinely public dataset may explicitly use `{"kind": "public", "evidence": "Reason access partitioning is unnecessary"}`. An absent scope or an unreviewed source adds a zero-row guard. All conditions are parenthesized, so a configured `OR` cannot bypass that guard.

## Fixture checks

Each case supplies all declared parameter values and at least one assertion: `expected_count`, `expected_columns` or `expected_rows`. Expected row objects may assert selected columns; comparison does not depend on database iteration order. Cases can replace a table's fictional rows using `rows`.

Fixture databases exist only in memory and are closed after each case. An authorizer blocks data changes and file/extension functions during assertions. Queries and result sizes have limits. This is a testing boundary, not a general-purpose SQL sandbox.

No fixtures means `not_provided`, not a successful functional test. Generated reviews always remain pending even when all fixture cases pass.

## Output and reproducibility

```text
.artifacts/<report-id>/<fingerprint>/
  query.sql
  standalone.sql
  definition.json
  review.json
  checks.json
  manifest.json
```

The manifest records the generator version, input hashes, dialect and artifact hashes. There are no source archives, absolute input paths or timestamps in bundles. Hashes detect drift, not authenticity. Reproduction requires the original input files, so keep real inputs in appropriate private storage.

`verify` without `--project` checks integrity only. With `--project`, it also rebuilds from those inputs and compares every output byte. Use the recorded generator version for reproduction. Increment `__version__` when changing generation semantics.

## Publishing

See [PUBLISHING.md](PUBLISHING.md) before creating a personal repository. The distribution contains source code, tests and fictional examples only. Generated artifacts and private inputs are ignored by default. The local `audit` command checks a limited set of credential patterns and reports locations without printing matching values; it is not a guarantee that a directory is free of confidential information or security issues.

No license is selected automatically. The repository owner should choose a license they are authorized to apply. No remote is configured and nothing is automatically committed or uploaded.
