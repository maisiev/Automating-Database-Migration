#!/usr/bin/env python3
"""Create reviewable SQL drafts from an Excel/CSV field mapping. Never execute SQL."""
import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import sqlglot
from sqlglot import exp


SIMPLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SENTINELS = {"", "NULL", "NONE", "0"}
DIRECTION = "oc-to-ms"
DIALECT = "tsql"
DRAFT_HEADER = """/* MIGRATION DRAFT - REVIEW REQUIRED
Review items are marked with REVIEW comments and listed in review.json.
NULL AS keeps an output column while its uncertain value is disabled.
Uncertain joins and filters are commented out/disabled for testing and may change results.
Do not ignore these items: review them with someone who understands the database
structure before relying on this query or its results.
*/

"""


@dataclass(frozen=True)
class Field:
    row: int
    table: str
    source: str
    target: str
    target_table: str = ""
    target_alias: str = ""
    review_note: str = ""


def text(value):
    return "" if value is None else str(value).strip()


def identifier(value):
    return isinstance(value, str) and bool(SIMPLE.fullmatch(value)) and value.upper() not in SENTINELS


def load_mapping(path, sheet=None):
    """Read all rows; UpdateChanges has no assumed meaning and is not a filter."""
    if path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.reader(handle))
    elif path.suffix.lower() == ".xlsx":
        from openpyxl import load_workbook
        book = load_workbook(path, read_only=True, data_only=True)
        try:
            if sheet is None and len(book.sheetnames) != 1:
                raise ValueError("Workbook has multiple sheets; specify --sheet.")
            rows = list((book[sheet] if sheet else book.active).values)
        finally:
            book.close()
    else:
        raise ValueError("Mapping must be .xlsx or .csv")
    if not rows:
        raise ValueError("Mapping is empty")
    headers = [text(v) for v in rows[0]]
    required = {"MSColumn", "OCTable", "OCColumn"}
    if not required.issubset(headers):
        raise ValueError("Mapping headers must contain MSColumn, OCTable, OCColumn")
    if len(headers) != len(set(headers)):
        raise ValueError("Duplicate mapping headers")
    source, target = "OCColumn", "MSColumn"
    fields, issues = [], []
    for row_number, row in enumerate(rows[1:], 2):
        record = dict(zip(headers, map(text, row)))
        if not any(record.values()):
            continue
        move_table = record.get("MSTable", "")
        move_alias = record.get("MSAlias", "")
        if (move_table and not identifier(move_table)) or (move_alias and not identifier(move_alias)):
            raise ValueError(f"Invalid destination table/alias in mapping row {row_number}")
        if bool(move_table) != bool(move_alias):
            raise ValueError(f"Mapping row {row_number} needs both MSTable and MSAlias")
        note = record.get("ReviewNote", "")
        if record.get("ReviewRequired", "").casefold() in {"1", "true", "yes"}:
            note = note or "Provisional mapping; supervisor review required"
        field = Field(row_number, record.get("OCTable", ""), record.get(source, ""), record.get(target, ""),
                      move_table, move_alias, note)
        if not identifier(field.table) or not identifier(field.source):
            issues.append({"row": row_number, "reason": "Source/table is blank, a sentinel, or an expression; review manually"})
            continue
        fields.append(field)
        if not identifier(field.target):
            issues.append({"row": row_number, "reason": "Target is blank, a sentinel, or an expression; no automatic replacement"})
    if not fields:
        raise ValueError("No usable source identifiers in mapping")
    return fields, issues


def mask_templates(sql, template_spans=None):
    """Hide report placeholders from the SQL parser without shifting offsets.

    Quoted SQL text/identifiers and comments are skipped. Original SQL is always
    the edit target. Optionally collect active placeholder spans for draft edits.
    """
    masked = list(sql)
    position = 0
    while position < len(sql):
        if sql.startswith("--", position):
            end = sql.find("\n", position + 2)
            position = len(sql) if end < 0 else end
        elif sql.startswith("/*", position):
            depth = 1
            position += 2
            while position < len(sql) and depth:
                if sql.startswith("/*", position):
                    depth += 1
                    position += 2
                elif sql.startswith("*/", position):
                    depth -= 1
                    position += 2
                else:
                    position += 1
        elif sql[position] in "'\"[`":
            closing = "]" if sql[position] == "[" else sql[position]
            position += 1
            while position < len(sql):
                if sql[position] == closing:
                    position += 1
                    if position < len(sql) and sql[position] == closing:
                        position += 1
                    else:
                        break
                else:
                    position += 1
        elif sql.startswith("{{", position):
            end = sql.find("}}", position + 2)
            if end < 0 or "{{" in sql[position + 2:end]:
                raise ValueError("Unclosed or nested report template placeholder")
            end += 2
            if template_spans is not None:
                template_spans.append((position, end))
            for index in range(position, end):
                if sql[index] not in "\r\n":
                    masked[index] = " "
            position = end
        else:
            position += 1
    return "".join(masked)


def join_insertion_position(sql, dialect):
    """Find the end of a simple SELECT's FROM/JOIN section using SQL tokens."""
    depth, in_from = 0, False
    for token in sqlglot.Dialect.get_or_raise(dialect).tokenize(mask_templates(sql)):
        word = token.token_type.name
        if word == "L_PAREN":
            depth += 1
        elif word == "R_PAREN":
            depth -= 1
        elif depth == 0:
            if word == "FROM":
                in_from = True
            elif in_from and word in {"WHERE", "GROUP_BY", "HAVING", "ORDER_BY", "QUALIFY", "LIMIT", "OFFSET", "FETCH", "FOR", "OPTION", "SEMICOLON"}:
                return token.start
    return len(sql)


def select_and_where_ranges(sql, statement, dialect):
    """Locate projection and WHERE spans in a supported simple SELECT."""
    tokens = sqlglot.Dialect.get_or_raise(dialect).tokenize(mask_templates(sql))
    starts = [node.meta["start"] for node in statement.walk() if "start" in node.meta]
    if not starts:
        return [], None
    anchor = min(starts)
    selected = [n for n, token in enumerate(tokens) if token.token_type.name == "SELECT" and token.start <= anchor]
    if not selected:
        return [], None
    begin = selected[-1] + 1
    # Preserve SELECT modifiers outside the replaced projection spans.
    while begin < len(tokens):
        if tokens[begin].token_type.name in {"DISTINCT", "ALL"}:
            begin += 1
        elif tokens[begin].text.upper() == "TOP":
            begin += 1
            if tokens[begin].token_type.name == "L_PAREN":
                depth = 1
                begin += 1
                while begin < len(tokens) and depth:
                    depth += (tokens[begin].token_type.name == "L_PAREN") - (tokens[begin].token_type.name == "R_PAREN")
                    begin += 1
            else:
                begin += 1
            if begin < len(tokens) and tokens[begin].text.upper() == "PERCENT":
                begin += 1
            if begin + 1 < len(tokens) and tokens[begin].text.upper() == "WITH" and tokens[begin + 1].text.upper() == "TIES":
                begin += 2
        else:
            break
    depth, segment, ranges = 0, begin, []
    where_start = None
    last_where_token = None
    for index in range(begin, len(tokens)):
        token = tokens[index]
        kind = token.token_type.name
        if depth == 0 and kind == "SEMICOLON":
            break
        if where_start is not None and depth == 0 and kind in {"GROUP_BY", "HAVING", "ORDER_BY", "QUALIFY", "LIMIT", "OFFSET", "FETCH", "FOR", "OPTION"}:
            break
        if depth == 0 and segment is not None and kind in {"COMMA", "FROM", "INTO"}:
            if index > segment:
                ranges.append((tokens[segment].start, tokens[index - 1].end + 1))
            segment = index + 1 if kind == "COMMA" else None
        if depth == 0 and kind == "WHERE":
            where_start = token.start
        if where_start is not None:
            last_where_token = token
        if kind == "L_PAREN":
            depth += 1
        elif kind == "R_PAREN":
            depth -= 1
    if segment is not None:
        end = index if tokens[index].token_type.name == "SEMICOLON" else index + 1
        if end > segment:
            ranges.append((tokens[segment].start, tokens[end - 1].end + 1))
    where_range = (where_start, last_where_token.end + 1) if where_start is not None and last_where_token else None
    return ranges, where_range


def join_ranges(sql, statement, dialect):
    """Locate complete explicit JOIN clauses, including their ON expressions."""
    tokens = sqlglot.Dialect.get_or_raise(dialect).tokenize(mask_templates(sql))
    starts = [n.meta["start"] for n in statement.walk() if "start" in n.meta]
    if not starts:
        return {}
    anchor = min(starts)
    selected = [i for i, t in enumerate(tokens) if t.token_type.name == "SELECT" and t.start <= anchor]
    if not selected:
        return {}
    boundaries, depth, end = [], 0, len(tokens)
    for index in range(selected[-1] + 1, len(tokens)):
        kind = tokens[index].token_type.name
        if depth == 0 and kind in {"WHERE", "GROUP_BY", "HAVING", "ORDER_BY", "QUALIFY", "LIMIT", "OFFSET", "FETCH", "FOR", "OPTION", "SEMICOLON"}:
            end = index
            break
        if depth == 0 and kind == "JOIN":
            start = index
            while start > 0 and tokens[start - 1].token_type.name in {"LEFT", "RIGHT", "FULL", "INNER", "OUTER", "CROSS"}:
                start -= 1
            boundaries.append(start)
        if kind == "L_PAREN":
            depth += 1
        elif kind == "R_PAREN":
            depth -= 1
    spans = [(tokens[a].start, tokens[b - 1].end + 1) for a, b in zip(boundaries, boundaries[1:] + [end]) if b > a]
    result = {}
    for join in statement.args.get("joins", []):
        if isinstance(join.this, exp.Table):
            position = join.this.this.meta.get("start", -1)
            for start, finish in spans:
                if start <= position < finish:
                    result[id(join)] = (start, finish)
    return result


def convert(sql, fields, dialect=DIALECT, table_map=None, ignore_case=False):
    """Patch only parser-identified identifier spans, preserving the source text."""
    table_map = table_map or {}
    if "\x00" in sql:
        raise ValueError("SQL contains NUL characters; export as UTF-8 text before conversion")
    norm = str.casefold if ignore_case else lambda s: s
    mapping = {}
    for field in fields:
        mapping.setdefault((norm(field.table), norm(field.source)), []).append(field)
    config = {norm(k): v for k, v in table_map.items()}
    issues, changes, patches, patch_records = [], [], {}, {}

    def issue(reason, node=None, **details):
        meta = node.meta if node is not None else {}
        issues.append({"reason": reason, "line": meta.get("line"), "column": meta.get("col"), **details})

    def patch(node, replacement, kind):
        meta = node.meta
        if "start" not in meta or "end" not in meta:
            raise ValueError("Parser did not provide a source position")
        start, end = meta["start"], meta["end"] + 1
        original = sql[start:end]
        if original == replacement:
            return
        key = (start, end)
        if key in patches and patches[key] != replacement:
            raise ValueError("Conflicting replacements")
        if key not in patches:
            record = {"kind": kind, "line": meta.get("line"), "column": meta.get("col"), "old": original, "new": replacement}
            changes.append(record)
            patch_records[key] = record
        patches[key] = replacement

    def discard_patches(start, end):
        for key in list(patches):
            if start <= key[0] and key[1] <= end:
                del patches[key]
                record = patch_records.pop(key, None)
                if record in changes:
                    changes.remove(record)

    template_spans = []
    masked_sql = mask_templates(sql, template_spans)
    statements = sqlglot.parse(masked_sql, read=dialect, error_level=sqlglot.ErrorLevel.RAISE)
    if any(f.target_table for f in fields) and len([s for s in statements if s is not None]) > 1:
        raise ValueError("Cross-table mappings require one SELECT per input file")
    if "{{" in sql or "}}" in sql:
        issue("Report template markers remain in the draft; review filtering and any field references inside them manually")
    comment_template = re.compile(r"\{\{\s*AND\s*\|\s*(?:i\.user_ctrl_lvls|o\.user_reporting_omnis)\s*\}\}", re.IGNORECASE)
    for start, end in template_spans:
        original = sql[start:end]
        if comment_template.fullmatch(original):
            replacement = "/*" + original + "*/"
            patches[(start, end)] = replacement
            line = sql.count("\n", 0, start) + 1
            column = start - sql.rfind("\n", 0, start)
            changes.append({"kind": "commented report placeholder", "line": line,
                            "column": column, "old": original, "new": replacement})
            issues.append({"reason": "Report placeholder commented out in draft; supervisor review required for filtering behavior",
                           "line": line, "column": column, "placeholder": original, "applied_to_draft": True})
    for statement in statements:
        if statement is None:
            continue
        # Fail closed for constructs needing lineage/schema information.
        if (not isinstance(statement, exp.Select)
                or len(list(statement.find_all(exp.Select))) != 1
                or statement.find(exp.With, exp.Subquery, exp.Union, exp.Intersect, exp.Except,
                                  exp.Pivot, exp.Lateral, exp.Unnest)
                or statement.args.get("into")
                or any(j.args.get("using") or j.args.get("method") == "NATURAL" for j in statement.find_all(exp.Join))):
            issue("Unsupported statement (CTE, subquery, set operation, DML, SELECT INTO, or USING/NATURAL join); statement unchanged")
            continue
        tables = list(statement.find_all(exp.Table))
        if any(not isinstance(t.this, exp.Identifier) for t in tables):
            issue("Table-valued functions require manual review; statement unchanged")
            continue
        sources = {}
        for table in tables:
            entry = config.get(norm(table.name), {})
            context = entry.get("mapping_table", table.name)
            alias = norm(table.alias_or_name)
            if alias in sources:
                raise ValueError("Duplicate table alias")
            sources[alias] = (table, context, entry.get("new_table"))
            if entry.get("review_note"):
                issue(entry["review_note"], table.this, table=table.name)
            if not any(key[0] == norm(context) for key in mapping):
                issue("Table has no mapping context; configure --table-map", table.this, table=table.name, mapping_table=context)

        final_aliases = {}
        for alias, (table, _, new_table) in sources.items():
            entry = config.get(norm(table.name), {})
            new_alias = entry.get("new_alias", table.alias or new_table or table.name)
            if norm(new_alias) in final_aliases:
                raise ValueError("Destination table aliases collide")
            final_aliases[norm(new_alias)] = (table, new_table or table.name)
            if entry.get("new_alias") and not table.alias:
                raise ValueError("new_alias currently requires an existing source table alias")
        site_join = None

        # A join-key change is scoped to its exact pair of tables and columns;
        # the same StoreId column can still join Item to Store elsewhere.
        join_overrides = {}
        for join in statement.args.get("joins", []):
            joined = join.this
            if not isinstance(joined, exp.Table):
                continue
            rule = config.get(norm(joined.name), {}).get("join_key_override")
            on = join.args.get("on")
            if not rule or on is None:
                continue
            matched = False
            for equality in on.find_all(exp.EQ):
                if not isinstance(equality.this, exp.Column) or not isinstance(equality.expression, exp.Column):
                    continue
                for own, other in [(equality.this, equality.expression), (equality.expression, equality.this)]:
                    other_source = sources.get(norm(other.table))
                    if (norm(own.table) == norm(joined.alias_or_name)
                            and norm(own.name) == norm(rule["source_column"])
                            and norm(other.name) == norm(rule["other_source_column"])
                            and other_source is not None
                            and norm(other_source[2] or other_source[0].name) == norm(rule["other_table"])):
                        join_overrides[id(own)] = rule["target_column"]
                        join_overrides[id(other)] = rule["other_target_column"]
                        matched = True
            if matched:
                issue("Configured join-key correction applied; verify tenant predicates, Area key and row counts", joined.this,
                      joined_table=joined.name, target_key=rule["target_column"])
            else:
                issue("Configured join-key pattern not found; join needs manual review", joined.this)

        explicit_aliases = {norm(p.alias) for p in statement.expressions if p.alias}
        for column in statement.find_all(exp.Column):
            if not column.table and norm(column.name) in explicit_aliases and column.find_ancestor(exp.Order, exp.Group, exp.Having, exp.Qualify):
                issue("Possible output alias reference; left unchanged for review", column.this, name=column.name)
                continue
            if column.table:
                candidates = [sources[norm(column.table)]] if norm(column.table) in sources else []
            elif len(sources) == 1:
                candidates = list(sources.values())
            else:
                issue("Unqualified column in a join; qualify it before conversion", column.this, name=column.name)
                continue
            if len(candidates) != 1:
                issue("Cannot resolve column's source table", column.this, name=column.sql(dialect=dialect))
                continue
            table, context, new_table = candidates[0]
            entry = config.get(norm(table.name), {})
            qualifier = entry.get("new_alias") or (new_table if not table.alias else None)
            if qualifier and column.table:
                patch(column.args["table"], exp.to_identifier(qualifier, quoted=True).sql(dialect=dialect), "table qualifier")
            if column.is_star:
                continue
            if id(column) in join_overrides:
                patch(column.this, exp.to_identifier(join_overrides[id(column)], quoted=True).sql(dialect=dialect), "join key")
                continue
            rows = mapping.get((norm(context), norm(column.name)), [])
            targets = {(row.target, row.target_table, row.target_alias) for row in rows}
            if not rows:
                issue("Column missing from mapping", column.this, table=context, name=column.name)
                continue
            if len(targets) != 1 or not all(identifier(t[0]) for t in targets):
                issue("Conflicting or non-identifier mapping; review manually", column.this,
                      table=context, name=column.name, mapping_rows=[r.row for r in rows], targets=sorted(targets))
                continue
            target, target_table, target_alias = next(iter(targets))
            if target_table:
                if not column.table or column.db or column.catalog:
                    issue("Cross-table reference needs a simple qualified source column; left unchanged", column.this)
                    continue
                existing = final_aliases.get(norm(target_alias))
                if existing and norm(existing[1]) != norm(target_table):
                    issue("Destination alias belongs to another table; cross-table mapping not applied", column.this)
                    continue
                if not existing:
                    stores = [(alias, t) for alias, (t, name) in final_aliases.items() if norm(name) == norm("Store")]
                    if norm(target_table) != norm("Site") or norm(target_alias) != norm("si") or len(stores) != 1:
                        issue("Cannot add Site join: exactly one Store source and destination Site si required; field move not applied", column.this)
                        continue
                    store_table = stores[0][1]
                    store_alias = config.get(norm(store_table.name), {}).get("new_alias") or store_table.alias_or_name
                    if not store_table.alias:
                        store_alias = config.get(norm(store_table.name), {}).get("new_table", store_table.name)
                    site_join = (target_table, target_alias, store_alias)
                # Override the regular source qualifier patch for this moved field.
                meta = column.args["table"].meta
                key = (meta["start"], meta["end"] + 1)
                if key in patches:
                    del patches[key]
                    changes[:] = [c for c in changes if not (c["line"] == meta.get("line") and c["column"] == meta.get("col") and c["kind"] == "table qualifier")]
                patch(column.args["table"], exp.to_identifier(target_alias, quoted=True).sql(dialect=dialect), "cross-table qualifier")
                issue("Cross-table mapping applied to draft; verify source, destination, and Site join", column.this,
                      mapping_rows=[r.row for r in rows], destination=f"{target_alias}.{target}")
            for row in rows:
                if row.review_note:
                    issue(row.review_note, column.this, mapping_row=row.row, applied_to_draft=True)
            rendered = exp.to_identifier(target, quoted=True).sql(dialect=dialect)
            if target != column.name:
                patch(column.this, rendered, "column")
                # Keep report-facing labels and ORDER BY references stable.
                if column.parent is statement and column in statement.expressions:
                    end = column.this.meta["end"] + 1
                    label = exp.to_identifier(column.name, quoted=True).sql(dialect=dialect)
                    patches[(end, end)] = " AS " + label

        for projection in statement.expressions:
            if not isinstance(projection, (exp.Column, exp.Alias, exp.Star)) and projection.find(exp.Column):
                issue("Computed output has no explicit alias; check the report-facing label after conversion")

        flagged = {(i["line"], i["column"]) for i in issues if i.get("line") is not None}
        def needs_review(node):
            return any((c.this.meta.get("line"), c.this.meta.get("col")) in flagged
                       for c in node.find_all(exp.Column) if not c.is_star)

        spans = join_ranges(sql, statement, dialect)
        disabled_joins, disabled_aliases = {}, set()
        joins = statement.args.get("joins", [])
        changed = True
        while changed:
            changed = False
            for join in joins:
                if id(join) in disabled_joins or not isinstance(join.this, exp.Table):
                    continue
                table = join.this
                source = sources.get(norm(table.alias_or_name))
                unknown_table = source is None or (not source[2] and not any(key[0] == norm(source[1]) for key in mapping))
                on = join.args.get("on")
                unknown_on = on is not None and needs_review(on)
                depends_on_disabled = on is not None and any(norm(c.table) in disabled_aliases for c in on.find_all(exp.Column) if c.table)
                if not (unknown_table or unknown_on or depends_on_disabled):
                    continue
                if id(join) not in spans:
                    raise ValueError("Cannot safely locate unresolved join; use explicit JOIN syntax for manual review")
                disabled_joins[id(join)] = join
                disabled_aliases.add(norm(table.alias_or_name))
                issue("Unresolved join commented out in draft; verify results without this join", table.this,
                      alias=table.alias_or_name, disabled_in_draft=True)
                changed = True
        if disabled_aliases:
            for column in statement.find_all(exp.Column):
                if column.table and norm(column.table) in disabled_aliases:
                    if column.is_star:
                        raise ValueError("Cannot preserve output columns for wildcard on a disabled join; enumerate columns first")
                    if column.find_ancestor(exp.Order, exp.Group, exp.Having, exp.Qualify, exp.Window):
                        raise ValueError("Disabled join alias is used in grouping, ordering, HAVING or a window; review that clause manually")
                    issue("Column belongs to a commented-out join; disable its output or filter", column.this,
                          alias=column.table, disabled_in_draft=True)
                    flagged.add((column.this.meta.get("line"), column.this.meta.get("col")))

        projection_ranges, where_range = select_and_where_ranges(sql, statement, dialect)
        if len(projection_ranges) != len(statement.expressions):
            issue("Could not safely locate SELECT fields for NULL placeholders; review manually")
        else:
            for index, (projection, (start, end)) in enumerate(zip(statement.expressions, projection_ranges), 1):
                if not needs_review(projection):
                    continue
                label = projection.alias_or_name or f"review_field_{index}"
                original = sql[start:end]
                comment = original.replace("*/", "* / ").replace("/*", "/ *")
                replacement = f"NULL AS {exp.to_identifier(label, quoted=True).sql(dialect=dialect)} /* REVIEW: {comment} */"
                discard_patches(start, end)
                patches[(start, end)] = replacement
                changes.append({"kind": "NULL placeholder for review field", "line": sql.count("\n", 0, start) + 1,
                                "column": start - sql.rfind("\n", 0, start), "old": original, "new": replacement})
                for record in issues:
                    if any((c.this.meta.get("line"), c.this.meta.get("col")) == (record.get("line"), record.get("column")) for c in projection.find_all(exp.Column)):
                        record["disabled_in_draft"] = True
                        if "applied_to_draft" in record:
                            record["applied_to_draft"] = False
                issue("Flagged SELECT output replaced with NULL; original expression retained in SQL comment", output=label)

        where = statement.args.get("where")
        if where is not None and where_range is not None and needs_review(where):
            def rewrite_predicate(node):
                if isinstance(node, exp.Paren) and isinstance(node.this, exp.And):
                    copied = node.copy()
                    copied.set("this", rewrite_predicate(node.this))
                    return copied
                if isinstance(node, exp.And):
                    copied = node.copy()
                    copied.set("this", rewrite_predicate(node.this))
                    copied.set("expression", rewrite_predicate(node.expression))
                    return copied
                if needs_review(node):
                    positions = {(c.this.meta.get("line"), c.this.meta.get("col")) for c in node.find_all(exp.Column)}
                    for record in issues:
                        if (record.get("line"), record.get("column")) in positions:
                            record["disabled_in_draft"] = True
                            if "applied_to_draft" in record:
                                record["applied_to_draft"] = False
                    original = node.sql(dialect=dialect).replace("*/", "* / ").replace("/*", "/ *")
                    replacement = exp.Paren(this=exp.EQ(this=exp.Literal.number(1), expression=exp.Literal.number(1)))
                    replacement.add_comments([f" REVIEW: disabled predicate: {original} "])
                    issue("Flagged WHERE predicate disabled with 1 = 1; original retained in SQL comment", predicate=original)
                    return replacement
                copied = node.copy()
                for identifier_node in list(copied.find_all(exp.Identifier)):
                    meta = identifier_node.meta
                    key = (meta.get("start"), meta.get("end", -1) + 1)
                    if key in patches:
                        identifier_node.replace(sqlglot.parse_one("SELECT " + patches[key], read=dialect).expressions[0].this.copy())
                return copied
            replacement = "WHERE " + rewrite_predicate(where.this).sql(dialect=dialect)
            start, end = where_range
            original = sql[start:end]
            # Retain requested placeholder comments that lie inside the rewritten WHERE.
            comments = [patches[(a, b)] for a, b in template_spans
                        if start <= a and b <= end and (a, b) in patches]
            if comments:
                replacement += " " + " ".join(comments)
            discard_patches(start, end)
            patches[(start, end)] = replacement
            changes.append({"kind": "disabled review WHERE predicates", "line": None, "column": None, "old": original, "new": replacement})

        # Do not add a provisional join solely for SELECT fields now set to NULL.
        if site_join and not any(c["kind"] == "cross-table qualifier" for c in changes):
            site_join = None

        for star in statement.find_all(exp.Star):
            if not isinstance(star.parent, exp.Count):
                issue("Wildcard requires schema review; columns cannot be enumerated", star)
        for table, _, new_table in sources.values():
            if new_table:
                patch(table.this, exp.to_identifier(new_table, quoted=True).sql(dialect=dialect), "table")
            new_alias = config.get(norm(table.name), {}).get("new_alias")
            if new_alias and table.alias:
                patch(table.args["alias"].this, exp.to_identifier(new_alias, quoted=True).sql(dialect=dialect), "table alias")
        for join_id, join in disabled_joins.items():
            start, end = spans[join_id]
            original = sql[start:end]
            rendered = original
            for (a, b), replacement in sorted(patches.items(), reverse=True):
                if start <= a and b <= end:
                    rendered = rendered[:a - start] + replacement + rendered[b - start:]
            comment = rendered.replace("/*", "/ *").replace("*/", "* /")
            replacement = "/*" + comment + "*/ /* REVIEW: unresolved join disabled */"
            discard_patches(start, end)
            patches[(start, end)] = replacement
            changes.append({"kind": "commented unresolved join", "line": sql.count("\n", 0, start) + 1,
                            "column": start - sql.rfind("\n", 0, start), "old": original, "new": replacement})
        if site_join:
            table_name, alias, store_alias = site_join
            quote = lambda value: exp.to_identifier(value, quoted=True).sql(dialect=dialect)
            qalias, qstore = quote(alias), quote(store_alias)
            addition = (f"\n/* REVIEW REQUIRED: proposed Site join; verify keys and row counts. */\n"
                        f"INNER JOIN {quote(table_name)} AS {qalias}\n"
                        f"    ON {qalias}.{quote('SiteId')} = {qstore}.{quote('SiteId')}\n"
                        f"   AND {qalias}.{quote('TenantId')} = {qstore}.{quote('TenantId')}\n")
            position = join_insertion_position(sql, dialect)
            patches[(position, position)] = addition
            changes.append({"kind": "provisional Site join", "line": None, "column": None, "old": "", "new": addition})
            issue("Proposed INNER Site join added from supplied new-query example; verify join type, keys, tenant handling and row counts")
            issue("Review Area join and tenant predicates separately; table renaming does not rewrite old join conditions")

    for (start, end), replacement in sorted(patches.items(), reverse=True):
        sql = sql[:start] + replacement + sql[end:]
    return sql, changes, issues


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="SQL file or folder (searched recursively)")
    parser.add_argument("--mapping", type=Path, required=True)
    parser.add_argument("--sheet")
    parser.add_argument("--table-map", type=Path, help="JSON: old table -> {mapping_table, new_table (optional)}")
    parser.add_argument("--ignore-case", action="store_true", help="Opt in to case-insensitive table/column matching")
    parser.add_argument("--output", type=Path, help="NEW output directory; never overwrites an existing directory")
    parser.add_argument("--dry-run", action="store_true", help="Print JSON report without writing anything")
    args = parser.parse_args(argv)
    try:
        if not args.dry_run and not args.output:
            raise ValueError("Specify --output or --dry-run")
        fields, mapping_issues = load_mapping(args.mapping, sheet=args.sheet)
        table_map = json.loads(args.table_map.read_text(encoding="utf-8")) if args.table_map else {}
        if not isinstance(table_map, dict):
            raise ValueError("Table map must be a JSON object")
        for old, entry in table_map.items():
            if not isinstance(entry, dict) or not identifier(entry.get("mapping_table", "")):
                raise ValueError(f"Table-map entry {old!r} needs a simple mapping_table identifier")
            if "new_table" in entry and not identifier(entry["new_table"]):
                raise ValueError("new_table must be a single identifier, not schema.table")
            if "new_alias" in entry and not identifier(entry["new_alias"]):
                raise ValueError("new_alias must be a simple identifier")
            if "join_key_override" in entry:
                rule = entry["join_key_override"]
                keys = ("other_table", "source_column", "other_source_column", "target_column", "other_target_column")
                if not isinstance(rule, dict) or not all(identifier(rule.get(key)) for key in keys):
                    raise ValueError("join_key_override needs simple table and column identifiers")
        if args.input.is_dir():
            files = sorted(p for p in args.input.rglob("*") if p.is_file() and p.suffix.lower() == ".sql")
            root = args.input.resolve()
        elif args.input.is_file() and args.input.suffix.lower() == ".sql":
            files, root = [args.input], args.input.resolve().parent
        else:
            raise ValueError("Input must be an existing .sql file or directory")
        if not files:
            raise ValueError("No .sql files found")
        if args.output and not args.dry_run:
            output = args.output.resolve()
            if output == root or root in output.parents:
                raise ValueError("Output must be outside the input folder")
            output.mkdir(parents=True, exist_ok=False)
        report = {"direction": DIRECTION, "dialect": DIALECT,
                  "mapping_issues": mapping_issues, "files": []}
        review = False
        for path in files:
            relative = path.resolve().relative_to(root)
            item = {"file": str(relative)}
            try:
                original = path.read_bytes().decode("utf-8-sig")
                result, changes, issues = convert(original, fields, DIALECT, table_map, args.ignore_case)
                item.update(status="review_required" if issues else "draft", changes=changes, issues=issues)
                if not args.dry_run:
                    destination = output / "queries" / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes((DRAFT_HEADER + result).encode("utf-8"))
            except (ValueError, sqlglot.errors.SqlglotError, UnicodeError) as error:
                item.update(status="error", changes=[], issues=[{"reason": str(error)}])
            review |= item["status"] != "draft"
            report["files"].append(item)
        payload = json.dumps(report, indent=2, ensure_ascii=False)
        if args.dry_run:
            print(payload)
        else:
            (output / "review.json").write_text(payload + "\n", encoding="utf-8")
            count = sum(len(item["changes"]) for item in report["files"])
            print(f"Processed {len(files)} file(s); {count} replacements. Report: {output / 'review.json'}")
            print("All output SQL is a draft: review diffs and validate against the target database.")
        return 1 if review else 0
    except (OSError, ValueError, KeyError, sqlglot.errors.SqlglotError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
