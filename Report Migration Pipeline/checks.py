"""Declarative fixture checks, confined to short-lived in-memory databases."""

import sqlite3

from .compiler import compile_report
from .contracts import InvalidInput, check_value, shape, text, version


SQLITE_TYPES = {"text": "TEXT", "integer": "INTEGER", "number": "NUMERIC", "boolean": "INTEGER", "date": "TEXT", "datetime": "TEXT"}


def run_checks(report, schema, plan, spec=None):
    if spec is None:
        return {"status": "not_provided", "cases": [], "limits": "No fixture data supplied; generation alone is not a functional test."}
    shape(spec, ("format_version", "tables", "cases"))
    version(spec["format_version"])
    if not isinstance(spec["tables"], dict) or not isinstance(spec["cases"], list):
        raise InvalidInput("Fixture tables and cases have invalid types.")
    if set(spec["tables"]) - set(schema["tables"]):
        raise InvalidInput("Fixtures reference a table absent from the target schema.")
    result = []
    sql = compile_report(report, schema, plan, "sqlite")["query"]
    for case in spec["cases"]:
        shape(case, ("name", "parameters"), ("rows", "expected_count", "expected_rows", "expected_columns"))
        name = text(case["name"], "Fixture name")
        connection = None
        try:
            overrides = case.get("rows", {})
            if not isinstance(overrides, dict) or set(overrides) - set(schema["tables"]):
                raise InvalidInput("A case overrides an undeclared table.")
            if not isinstance(case["parameters"], dict) or set(case["parameters"]) != set(plan["parameters"]):
                raise InvalidInput("Fixture parameters must match the declared parameter contract.")
            for key, item in plan["parameters"].items():
                check_value(case["parameters"][key], item["type"])
            connection = sqlite3.connect(":memory:")
            disable_extensions = getattr(connection, "enable_load_extension", None)
            if disable_extensions is not None:
                disable_extensions(False)
            for table, columns in schema["tables"].items():
                connection.execute('CREATE TABLE "%s" (%s)' % (table, ", ".join('"%s" %s' % (name, SQLITE_TYPES[kind]) for name, kind in columns.items())))
                rows = overrides.get(table, spec["tables"].get(table, []))
                if not isinstance(rows, list) or len(rows) > 10000:
                    raise InvalidInput("Fixture tables need lists of at most 10,000 rows.")
                for row in rows:
                    if not isinstance(row, dict) or set(row) - set(columns):
                        raise InvalidInput("Fixture row contains undeclared fields.")
                    for key, value in row.items():
                        check_value(value, columns[key])
                    fields = list(row)
                    if fields:
                        connection.execute('INSERT INTO "%s" (%s) VALUES (%s)' % (table, ", ".join('"%s"' % k for k in fields), ", ".join("?" for _ in fields)), list(row.values()))
                    else:
                        connection.execute('INSERT INTO "%s" DEFAULT VALUES' % table)
            allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}

            def authorizer(action, one, two, database, trigger):
                if action == sqlite3.SQLITE_FUNCTION and str(two).lower() in ("load_extension", "readfile", "writefile"):
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY

            connection.set_authorizer(authorizer)
            counter = [0]

            def progress():
                counter[0] += 1
                return int(counter[0] > 1000)

            connection.set_progress_handler(progress, 1000)
            cursor = connection.execute(sql, case["parameters"])
            columns = [c[0] for c in cursor.description]
            raw = cursor.fetchmany(10001)
            if len(raw) > 10000:
                raise InvalidInput("Fixture results exceed the 10,000-row limit.")
            rows = [dict(zip(columns, row)) for row in raw]
            if not any(k in case for k in ("expected_count", "expected_rows", "expected_columns")):
                raise InvalidInput("Every fixture case must contain an assertion.")
            if "expected_count" in case and len(rows) != case["expected_count"]:
                raise InvalidInput("Expected row count did not match.")
            if "expected_columns" in case and columns != case["expected_columns"]:
                raise InvalidInput("Expected columns did not match.")
            if "expected_rows" in case:
                expected = case["expected_rows"]
                if not isinstance(expected, list) or len(expected) != len(rows):
                    raise InvalidInput("Expected rows did not match.")
                # Compare partial rows without depending on database iteration order.
                remaining = list(rows)
                for wanted in expected:
                    if not isinstance(wanted, dict):
                        raise InvalidInput("Expected rows must be objects.")
                    match = next((i for i, actual in enumerate(remaining) if all(k in actual and actual[k] == v for k, v in wanted.items())), None)
                    if match is None:
                        raise InvalidInput("Expected values did not match.")
                    remaining.pop(match)
            result.append({"name": name, "status": "passed"})
        except (InvalidInput, sqlite3.Error) as exc:
            result.append({"name": name, "status": "failed", "reason": str(exc)})
        finally:
            if connection is not None:
                connection.close()
    return {"status": "passed" if result and all(r["status"] == "passed" for r in result) else "failed" if result else "not_provided", "cases": result, "limits": "SQLite fixtures only; server compilation, production precision, access policy and business parity require independent review."}
