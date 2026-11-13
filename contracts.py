"""Vendor-neutral JSON contracts. No SQL, Python or shell input is evaluated."""

import hashlib
import json
import math
import re
from pathlib import Path


class InvalidInput(ValueError):
    pass


IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}\Z")
REPORT_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
TYPES = {"text", "integer", "number", "boolean", "date", "datetime"}


def identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise InvalidInput("Identifiers must contain only letters, numbers and underscores, starting with a letter or underscore.")
    return value


def shape(value, required, optional=()):
    if not isinstance(value, dict) or set(required) - set(value):
        raise InvalidInput("A JSON object is missing required properties: " + ", ".join(required))
    if set(value) - set(required) - set(optional):
        raise InvalidInput("Unexpected properties: " + ", ".join(sorted(set(value) - set(required) - set(optional))))


def unique(values):
    lowered = [v.lower() for v in values]
    if len(lowered) != len(set(lowered)):
        raise InvalidInput("Names must be unique, ignoring case.")


def logical_type(value):
    if value not in TYPES:
        raise InvalidInput("Unsupported logical type. Use text, integer, number, boolean, date or datetime.")
    return value


def text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise InvalidInput(label + " must be nonempty text.")
    return value


def version(value):
    if type(value) is not int or value != 1:
        raise InvalidInput("Only format_version 1 is supported.")


def json_bytes(value):
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise InvalidInput("Duplicate JSON properties are not permitted.")
        result[key] = value
    return result


def read_json(path):
    with Path(path).open("rb") as stream:
        raw = stream.read(4 * 1024 * 1024 + 1)
    if len(raw) > 4 * 1024 * 1024:
        raise InvalidInput("JSON input exceeds the 4 MiB limit.")
    try:
        value = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_pairs, parse_constant=lambda _: (_ for _ in ()).throw(InvalidInput("Non-finite JSON numbers are not permitted.")))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise InvalidInput("Input must be valid UTF-8 JSON.") from exc
    return value, raw


def check_report(report):
    shape(report, ("format_version", "id", "title", "columns"), ("description",))
    version(report["format_version"])
    if not isinstance(report["id"], str) or not REPORT_ID.fullmatch(report["id"]):
        raise InvalidInput("Report IDs use lowercase letters, numbers, underscores and hyphens.")
    text(report["title"], "Report title")
    if "description" in report and not isinstance(report["description"], str):
        raise InvalidInput("Description must be text.")
    if not isinstance(report["columns"], list) or not report["columns"]:
        raise InvalidInput("A report needs at least one column.")
    for column in report["columns"]:
        shape(column, ("id", "label", "type"), ("visible",))
        identifier(column["id"])
        text(column["label"], "Column label")
        logical_type(column["type"])
        if "visible" in column and type(column["visible"]) is not bool:
            raise InvalidInput("Column visibility must be boolean.")
    unique([c["id"] for c in report["columns"]])


def check_schema(schema):
    shape(schema, ("format_version", "tables"))
    version(schema["format_version"])
    if not isinstance(schema["tables"], dict):
        raise InvalidInput("Schema tables must be an object.")
    unique(list(schema["tables"]))
    for table, columns in schema["tables"].items():
        identifier(table)
        if not isinstance(columns, dict) or not columns:
            raise InvalidInput("Each schema table needs a column/type map.")
        unique(list(columns))
        for name, kind in columns.items():
            identifier(name)
            logical_type(kind)


def check_value(value, kind):
    if value is None:
        return
    if type(value) is int and not -(2 ** 63) <= value < 2 ** 63:
        raise InvalidInput("Integer values must fit the signed 64-bit range.")
    good = isinstance(value, str) if kind in ("text", "date", "datetime") else type(value) is bool if kind == "boolean" else type(value) is int if kind == "integer" else type(value) in (int, float) and math.isfinite(value)
    if not good:
        raise InvalidInput("A value does not match its declared logical type.")


def load_project(path):
    path = Path(path).resolve()
    project, raw = read_json(path)
    shape(project, ("format_version", "report", "schema", "plan"), ("fixtures", "dialect"))
    version(project["format_version"])
    if project.get("dialect", "sqlserver") not in ("sqlserver", "sqlite"):
        raise InvalidInput("Dialect must be sqlserver or sqlite.")
    inputs = {}
    hashes = {"project": digest(raw)}
    paths = [path]
    for role in ("report", "schema", "plan", "fixtures"):
        if role not in project:
            continue
        relative = project[role]
        if not isinstance(relative, str) or Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise InvalidInput("Project members must be relative files inside the project directory.")
        member = path.parent / relative
        if member.is_symlink() or path.parent not in member.resolve().parents or not member.is_file():
            raise InvalidInput("Missing or unsafe project member: " + role)
        inputs[role], contents = read_json(member)
        hashes[role] = digest(contents)
        paths.append(member.resolve())
    check_report(inputs["report"])
    check_schema(inputs["schema"])
    return {"inputs": inputs, "input_hashes": hashes, "dialect": project.get("dialect", "sqlserver"), "paths": paths}


def scaffold(report):
    check_report(report)
    return {
        "format_version": 1,
        "source": None,
        "source_review": "No target row source has been reviewed.",
        "joins": [], "parameters": {}, "scope": None, "where": [],
        "columns": [{"source": c["id"], "output": c["id"], "expression": None, "review": "No reviewed target mapping is available."} for c in report["columns"]],
    }
