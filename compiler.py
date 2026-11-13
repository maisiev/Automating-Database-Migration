"""Generate SELECT statements from a small, typed expression tree."""

import math

from .contracts import InvalidInput, check_report, check_schema, check_value, identifier, logical_type, shape, text, unique, version


SQL_TYPES = {
    "sqlserver": {"text": "nvarchar(4000)", "integer": "bigint", "number": "decimal(28,6)", "boolean": "bit", "date": "date", "datetime": "datetime2"},
    "sqlite": {"text": "TEXT", "integer": "INTEGER", "number": "NUMERIC", "boolean": "INTEGER", "date": "TEXT", "datetime": "TEXT"},
}


def compatible(a, b):
    return a == b or "null" in (a, b) or {a, b} <= {"integer", "number"}


class Renderer:
    def __init__(self, schema, parameters, dialect, literal_parameters=False):
        if dialect not in SQL_TYPES:
            raise InvalidInput("Unsupported SQL dialect.")
        self.schema = schema["tables"]
        self.parameters = parameters
        self.dialect = dialect
        self.relations = {}
        self.used_parameters = set()
        self.literal_parameters = literal_parameters

    def quote(self, value):
        identifier(value)
        return "[" + value + "]" if self.dialect == "sqlserver" else '"' + value + '"'

    def relation(self, item):
        shape(item, ("table", "alias"))
        table, alias = identifier(item["table"]), identifier(item["alias"])
        if table not in self.schema:
            raise InvalidInput("A source table is absent from the declared schema.")
        if alias.lower() in {a.lower() for a in self.relations}:
            raise InvalidInput("Relation aliases must be unique.")
        self.relations[alias] = table
        return self.quote(table) + " AS " + self.quote(alias)

    def literal(self, value):
        if value is None:
            return "NULL", "null"
        if type(value) is bool:
            return "1" if value else "0", "boolean"
        if type(value) is int:
            check_value(value, "integer")
            return str(value), "integer"
        if type(value) is float and math.isfinite(value):
            return repr(value), "number"
        if isinstance(value, str):
            return ("N" if self.dialect == "sqlserver" else "") + "'" + value.replace("'", "''") + "'", "text"
        raise InvalidInput("Only finite JSON scalar literals are supported.")

    def expression(self, node, depth=0):
        if depth > 24 or not isinstance(node, dict):
            raise InvalidInput("Invalid or excessively nested expression.")
        if set(node) == {"column"}:
            value = node["column"]
            if not isinstance(value, str) or len(value.split(".")) != 2:
                raise InvalidInput("Column expressions use alias.column.")
            alias, name = value.split(".")
            identifier(alias)
            identifier(name)
            if alias not in self.relations or name not in self.schema[self.relations[alias]]:
                raise InvalidInput("A referenced column is not declared for its relation.")
            return self.quote(alias) + "." + self.quote(name), self.schema[self.relations[alias]][name]
        if set(node) == {"parameter"}:
            name = identifier(node["parameter"])
            if name not in self.parameters:
                raise InvalidInput("An expression uses an undeclared parameter.")
            self.used_parameters.add(name)
            placeholder = self.literal(self.parameters[name]["default"])[0] if self.literal_parameters else ("@" if self.dialect == "sqlserver" else ":") + name
            return placeholder, self.parameters[name]["type"]
        if set(node) == {"literal"}:
            return self.literal(node["literal"])
        if "case" in node:
            shape(node, ("case", "else"))
            if not isinstance(node["case"], list) or not node["case"]:
                raise InvalidInput("CASE needs at least one branch.")
            branches = []
            fallback, result_type = self.expression(node["else"], depth + 1)
            for branch in node["case"]:
                shape(branch, ("when", "then"))
                condition, kind = self.expression(branch["when"], depth + 1)
                if kind != "predicate":
                    raise InvalidInput("CASE conditions must be predicates.")
                value, value_type = self.expression(branch["then"], depth + 1)
                if value_type == "predicate" or result_type == "predicate":
                    raise InvalidInput("CASE return values must be scalar expressions.")
                if not compatible(result_type, value_type):
                    raise InvalidInput("CASE branches have incompatible types.")
                result_type = value_type if result_type == "null" else "number" if "number" in (result_type, value_type) else result_type
                branches.append("WHEN %s THEN %s" % (condition, value))
            return "(CASE " + " ".join(branches) + " ELSE " + fallback + " END)", result_type
        shape(node, ("op", "args"))
        op, args = node["op"], node["args"]
        if not isinstance(args, list):
            raise InvalidInput("Operator args must be a list.")
        rendered = [self.expression(arg, depth + 1) for arg in args]
        values, kinds = [r[0] for r in rendered], [r[1] for r in rendered]
        if op in ("add", "subtract", "multiply", "divide"):
            if len(args) != 2 or not set(kinds) <= {"integer", "number", "null"}:
                raise InvalidInput("Arithmetic needs two numeric expressions.")
            if op == "divide":
                # Undefined quotients stay NULL. No denominator is invented.
                return "(1.0 * %s / NULLIF(%s, 0))" % tuple(values), "number"
            return "(%s %s %s)" % (values[0], {"add": "+", "subtract": "-", "multiply": "*"}[op], values[1]), "number" if "number" in kinds else "integer"
        if op in ("eq", "ne", "lt", "le", "gt", "ge"):
            if len(args) != 2 or "predicate" in kinds or not compatible(*kinds):
                raise InvalidInput("Comparison needs two compatible expressions.")
            symbol = {"eq": "=", "ne": "<>", "lt": "<", "le": "<=", "gt": ">", "ge": ">="}[op]
            if "null" in kinds:
                if op not in ("eq", "ne"):
                    raise InvalidInput("Use eq/ne for NULL comparisons.")
                other = values[1] if kinds[0] == "null" else values[0]
                return "(%s IS %sNULL)" % (other, "NOT " if op == "ne" else ""), "predicate"
            return "(%s %s %s)" % (values[0], symbol, values[1]), "predicate"
        if op in ("and", "or"):
            if len(args) < 2 or set(kinds) != {"predicate"}:
                raise InvalidInput("AND/OR need at least two predicates.")
            return "(" + (" " + op.upper() + " ").join(values) + ")", "predicate"
        if op == "not":
            if kinds != ["predicate"]:
                raise InvalidInput("NOT needs one predicate.")
            return "(NOT " + values[0] + ")", "predicate"
        if op in ("upper", "lower", "abs", "round", "coalesce"):
            if "predicate" in kinds:
                raise InvalidInput("Scalar functions cannot accept predicates.")
            if op in ("upper", "lower") and (len(args) != 1 or kinds[0] not in ("text", "null")):
                raise InvalidInput("Text functions need one text expression.")
            if op == "abs" and (len(args) != 1 or kinds[0] not in ("integer", "number", "null")):
                raise InvalidInput("ABS needs one numeric expression.")
            if op == "round" and (len(args) != 2 or kinds[0] not in ("integer", "number", "null") or kinds[1] != "integer"):
                raise InvalidInput("ROUND needs a number and integer precision.")
            nonnull = [k for k in kinds if k != "null"]
            if op == "coalesce" and (len(args) < 2 or nonnull and any(not compatible(nonnull[0], k) for k in nonnull[1:])):
                raise InvalidInput("COALESCE needs compatible expressions.")
            result_type = next((k for k in kinds if k != "null"), "null")
            if op == "coalesce" and "number" in kinds:
                result_type = "number"
            return op.upper() + "(" + ", ".join(values) + ")", result_type
        raise InvalidInput("Unsupported operator; arbitrary SQL/functions are not accepted.")


def compile_report(report, schema, plan, dialect="sqlserver", *, _manual=True, _literal_parameters=False):
    check_report(report)
    check_schema(schema)
    shape(plan, ("format_version", "source", "source_review", "columns", "parameters", "scope"), ("joins", "where"))
    version(plan["format_version"])
    text(plan["source_review"], "Source review/evidence")
    params = plan["parameters"]
    if not isinstance(params, dict):
        raise InvalidInput("Parameters must be an object.")
    unique(list(params))
    for name, item in params.items():
        identifier(name)
        shape(item, ("type", "default"))
        check_value(item["default"], logical_type(item["type"]))
    renderer = Renderer(schema, params, dialect, _literal_parameters)
    issues = []
    from_clause = ""
    if plan["source"] is None:
        issues.append({"field": None, "reason": plan["source_review"], "blocks_rows": True})
    else:
        shape(plan["source"], ("table", "alias", "reviewed"))
        if type(plan["source"]["reviewed"]) is not bool:
            raise InvalidInput("Source reviewed must be boolean.")
        from_clause = "\nFROM " + renderer.relation({k: plan["source"][k] for k in ("table", "alias")})
        if not plan["source"]["reviewed"]:
            issues.append({"field": None, "reason": plan["source_review"], "blocks_rows": True})
    joins = plan.get("joins", [])
    if not isinstance(joins, list) or joins and plan["source"] is None:
        raise InvalidInput("Joins require a declared base source.")
    for join in joins:
        shape(join, ("table", "alias", "kind", "on"))
        if join["kind"] not in ("inner", "left"):
            raise InvalidInput("Join kind must be inner or left.")
        table = renderer.relation({k: join[k] for k in ("table", "alias")})
        condition, kind = renderer.expression(join["on"])
        if kind != "predicate":
            raise InvalidInput("Join conditions must be predicates.")
        from_clause += "\n" + join["kind"].upper() + " JOIN " + table + " ON " + condition
    scope = plan["scope"]
    conditions = []
    if scope is not None and not isinstance(scope, dict):
        raise InvalidInput("scope must be an object or null.")
    if scope is None:
        issues.append({"field": None, "reason": "Access partitioning has not been reviewed.", "blocks_rows": True})
    elif scope.get("kind") == "public":
        shape(scope, ("kind", "evidence"))
        text(scope["evidence"], "Unscoped public-data evidence")
    else:
        shape(scope, ("column", "parameter"))
        name = identifier(scope["parameter"])
        if name not in params or params[name]["default"] is not None:
            raise InvalidInput("The scope parameter must be declared and default to NULL.")
        condition, _ = renderer.expression({"op": "eq", "args": [{"column": scope["column"]}, {"parameter": name}]})
        conditions.append(condition)
    if not isinstance(plan.get("where", []), list):
        raise InvalidInput("where must be a list of predicates.")
    for node in plan.get("where", []):
        condition, kind = renderer.expression(node)
        if kind != "predicate":
            raise InvalidInput("WHERE expressions must be predicates.")
        conditions.append(condition)
    if not isinstance(plan["columns"], list):
        raise InvalidInput("Plan columns must be a list.")
    source_ids, outputs = [], []
    for column in plan["columns"]:
        shape(column, ("source", "output", "expression"), ("evidence", "review"))
        source_ids.append(identifier(column["source"]))
        outputs.append(identifier(column["output"]))
    unique(source_ids)
    unique(outputs)
    if set(source_ids) != {c["id"] for c in report["columns"]}:
        raise InvalidInput("Every source report column needs exactly one plan entry.")
    mapping = {c["source"]: c for c in plan["columns"]}
    projection, metadata = [], []
    for source_column in report["columns"]:
        item = mapping[source_column["id"]]
        kind = source_column["type"]
        if item["expression"] is None:
            reason = text(item.get("review"), "Unmapped column review reason")
            expression = "CAST(NULL AS " + SQL_TYPES[dialect][kind] + ")"
            issues.append({"field": item["output"], "reason": reason, "blocks_rows": False})
            status = "unmapped"
        else:
            text(item.get("evidence"), "Mapping evidence")
            expression, actual = renderer.expression(item["expression"])
            if actual == "predicate" and kind == "boolean":
                expression = "(CASE WHEN %s THEN 1 WHEN NOT %s THEN 0 ELSE NULL END)" % (expression, expression)
                actual = "boolean"
            if actual != kind and actual != "null" and not (actual == "integer" and kind == "number"):
                raise InvalidInput("A mapped expression has an incompatible output type.")
            status = "mapped"
        projection.append("  " + expression + " AS " + renderer.quote(item["output"]))
        metadata.append({"id": item["output"], "label": source_column["label"], "type": kind, "visible": source_column.get("visible", True), "mapping_status": status})
    if set(params) != renderer.used_parameters:
        raise InvalidInput("Every declared parameter must be used; remove unused declarations.")
    guarded = any(i["blocks_rows"] for i in issues)
    conditions.append("1 = 0" if guarded else "1 = 1")
    query = "-- Generated review draft; semantic approval remains pending.\nSELECT\n" + ",\n".join(projection) + from_clause + "\nWHERE " + "\n  AND ".join("(" + c + ")" for c in conditions) + "\n"
    declarations = []
    for name, item in sorted(params.items()):
        value, _ = renderer.literal(item["default"])
        if dialect == "sqlserver":
            declarations.append("DECLARE @%s %s = %s;" % (name, SQL_TYPES[dialect][item["type"]], value))
    standalone = "-- Review defaults must be checked before use.\n"
    if not _manual:
        standalone = ""
    elif dialect == "sqlserver":
        standalone += "\n".join(declarations) + "\n\n" + query
    else:
        standalone += compile_report(report, schema, plan, dialect, _manual=False, _literal_parameters=True)["query"]
    return {
        "query": query,
        "standalone": standalone,
        "definition": {"format_version": 1, "id": report["id"], "title": report["title"], "description": report.get("description", ""), "columns": metadata, "parameters": params, "review_status": "pending"},
        "review": {"report_id": report["id"], "status": "pending", "guarded": guarded, "issues": issues, "scope": scope, "limits": ["Declared schemas and mapping evidence require independent verification.", "Generated SQL is not executed against a live server.", "Offline fixtures do not establish business parity or production SQL Server behavior."]},
    }
