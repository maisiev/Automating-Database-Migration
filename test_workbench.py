import contextlib
import copy
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from report_workbench.audit import audit_tree
from report_workbench.bundles import assemble, build, verify, write_new
from report_workbench.checks import run_checks
from report_workbench.cli import main
from report_workbench.compiler import compile_report
from report_workbench.contracts import InvalidInput, json_bytes, load_project, read_json, scaffold


ROOT = Path(__file__).resolve().parents[1]


def example():
    return copy.deepcopy(load_project(ROOT / "examples/project.json"))


class CompilerTests(unittest.TestCase):
    def setUp(self):
        self.project = example()
        self.data = self.project["inputs"]

    def compile(self, dialect="sqlserver"):
        return compile_report(self.data["report"], self.data["schema"], self.data["plan"], dialect)

    def test_synthetic_example_passes_all_fixture_cases(self):
        outcome = run_checks(self.data["report"], self.data["schema"], self.data["plan"], self.data["fixtures"])
        self.assertEqual(outcome["status"], "passed")
        self.assertEqual(len(outcome["cases"]), 4)

    def test_all_columns_and_labels_are_preserved(self):
        compiled = self.compile()
        self.assertEqual(len(compiled["definition"]["columns"]), 7)
        self.assertEqual([c["label"] for c in compiled["definition"]["columns"]], [c["label"] for c in self.data["report"]["columns"]])
        self.assertFalse(compiled["definition"]["columns"][-1]["visible"])

    def test_unknown_field_stays_null_without_removing_known_rows(self):
        compiled = self.compile()
        self.assertIn("CAST(NULL AS nvarchar(4000)) AS [legacy_note]", compiled["query"])
        self.assertFalse(compiled["review"]["guarded"])
        self.assertEqual(compiled["review"]["status"], "pending")

    def test_initializer_guesses_no_fields_or_row_sources(self):
        plan = scaffold(self.data["report"])
        compiled = compile_report(self.data["report"], self.data["schema"], plan)
        self.assertTrue(all(c["expression"] is None for c in plan["columns"]))
        self.assertTrue(compiled["review"]["guarded"])
        self.assertIn("1 = 0", compiled["query"])
        self.assertNotIn("FROM [products]", compiled["query"])

    def test_unreviewed_source_cannot_be_bypassed_by_or(self):
        self.data["plan"]["source"]["reviewed"] = False
        self.data["plan"]["where"] = [{"op": "or", "args": [{"op": "eq", "args": [{"literal": 1}, {"literal": 1}]}, {"op": "eq", "args": [{"literal": 2}, {"literal": 2}]}]}]
        self.data["fixtures"]["cases"] = [{"name": "guard wins", "parameters": {"workspace": "demo-a"}, "expected_count": 0}]
        self.assertEqual(run_checks(self.data["report"], self.data["schema"], self.data["plan"], self.data["fixtures"])["status"], "passed")

    def test_missing_access_scope_forces_guard(self):
        self.data["plan"]["scope"] = None
        self.data["plan"]["parameters"] = {}
        self.assertTrue(self.compile()["review"]["guarded"])

    def test_scope_cannot_default_to_a_real_workspace(self):
        self.data["plan"]["parameters"]["workspace"]["default"] = "demo-a"
        with self.assertRaisesRegex(InvalidInput, "default to NULL"):
            self.compile()

    def test_missing_target_field_is_rejected(self):
        self.data["plan"]["columns"][0]["expression"] = {"column": "p.unknown_field"}
        with self.assertRaisesRegex(InvalidInput, "not declared"):
            self.compile()

    def test_unknown_relation_is_rejected(self):
        self.data["plan"]["columns"][0]["expression"] = {"column": "missing.sku"}
        with self.assertRaises(InvalidInput):
            self.compile()

    def test_evidence_is_required_for_every_mapping(self):
        del self.data["plan"]["columns"][0]["evidence"]
        with self.assertRaises(InvalidInput):
            self.compile()

    def test_review_reason_is_required_for_unmapped_fields(self):
        del self.data["plan"]["columns"][-1]["review"]
        with self.assertRaises(InvalidInput):
            self.compile()

    def test_fields_cannot_be_silently_dropped(self):
        self.data["plan"]["columns"].pop()
        with self.assertRaisesRegex(InvalidInput, "Every source"):
            self.compile()

    def test_duplicate_output_aliases_are_rejected(self):
        self.data["plan"]["columns"][1]["output"] = "SKU"
        with self.assertRaisesRegex(InvalidInput, "unique"):
            self.compile()

    def test_sql_in_identifiers_is_rejected(self):
        for key in ("alias", "table"):
            with self.subTest(key=key):
                data = copy.deepcopy(self.data)
                data["plan"]["source"][key] = "products; DROP TABLE products"
                with self.assertRaises(InvalidInput):
                    compile_report(data["report"], data["schema"], data["plan"])

    def test_raw_sql_and_arbitrary_functions_are_rejected(self):
        for node in ({"sql": "SELECT 1"}, {"op": "load_extension", "args": [{"literal": "missing"}]}, "p.sku"):
            with self.subTest(node=node):
                self.data["plan"]["columns"][0]["expression"] = node
                with self.assertRaises(InvalidInput):
                    self.compile()

    def test_parameters_must_be_declared_and_used(self):
        self.data["plan"]["parameters"]["unused"] = {"type": "text", "default": None}
        with self.assertRaisesRegex(InvalidInput, "must be used"):
            self.compile()

    def test_numeric_narrowing_is_not_silently_accepted(self):
        self.data["report"]["columns"][3]["type"] = "integer"
        with self.assertRaisesRegex(InvalidInput, "incompatible"):
            self.compile()

    def test_cross_partition_join_error_is_detected_by_fixture_data(self):
        self.data["plan"]["joins"][0]["on"] = {"op": "eq", "args": [{"column": "p.aisle_id"}, {"column": "a.id"}]}
        self.assertEqual(run_checks(self.data["report"], self.data["schema"], self.data["plan"], self.data["fixtures"])["status"], "failed")

    def test_division_by_zero_remains_null(self):
        self.data["plan"]["columns"][3]["expression"] = {"op": "divide", "args": [{"column": "p.unit_price"}, {"column": "p.available"}]}
        spec = self.data["fixtures"]
        spec["tables"]["products"] = [{"workspace": "demo-a", "sku": "ZERO", "title": "Zero divisor", "available": 0, "unit_price": 2.0, "aisle_id": 10, "enabled": True}]
        spec["cases"] = [{"name": "undefined quotient", "parameters": {"workspace": "demo-a"}, "expected_rows": [{"sku": "ZERO", "stock_value": None}]}]
        self.assertEqual(run_checks(self.data["report"], self.data["schema"], self.data["plan"], spec)["status"], "passed")

    def test_case_arithmetic_is_portable_to_fixture_backend(self):
        self.data["plan"]["columns"][3]["expression"] = {"case": [{"when": {"op": "lt", "args": [{"column": "p.available"}, {"literal": 0}]}, "then": {"literal": 0}}], "else": {"op": "multiply", "args": [{"column": "p.available"}, {"column": "p.unit_price"}]}}
        self.assertEqual(run_checks(self.data["report"], self.data["schema"], self.data["plan"], self.data["fixtures"])["status"], "passed")

    def test_sqlite_standalone_uses_null_defaults_without_editing_literals(self):
        self.data["plan"]["columns"][1]["expression"] = {"literal": ":workspace"}
        compiled = self.compile("sqlite")
        self.assertIn("':workspace'", compiled["standalone"])
        connection = sqlite3.connect(":memory:")
        for name, columns in self.data["schema"]["tables"].items():
            connection.execute('CREATE TABLE "%s" (%s)' % (name, ", ".join('"%s" TEXT' % c for c in columns)))
        self.assertEqual(connection.execute(compiled["standalone"]).fetchall(), [])
        connection.close()

    def test_literals_are_escaped_as_data(self):
        value = "x'); DROP TABLE products;--"
        self.data["plan"]["columns"][1]["expression"] = {"literal": value}
        spec = self.data["fixtures"]
        spec["cases"] = [{"name": "literal is data", "parameters": {"workspace": "demo-b"}, "expected_rows": [{"name": value}]}]
        self.assertEqual(run_checks(self.data["report"], self.data["schema"], self.data["plan"], spec)["status"], "passed")

    def test_empty_fixture_cases_are_not_reported_as_passed(self):
        self.data["fixtures"]["cases"] = []
        self.assertEqual(run_checks(self.data["report"], self.data["schema"], self.data["plan"], self.data["fixtures"])["status"], "not_provided")

    def test_fixture_typo_is_not_silently_ignored(self):
        self.data["fixtures"]["cases"][0]["rows"] = {"misspelled": []}
        self.assertEqual(run_checks(self.data["report"], self.data["schema"], self.data["plan"], self.data["fixtures"])["status"], "failed")

    def test_fixture_parameter_contract_is_checked(self):
        self.data["fixtures"]["cases"][0]["parameters"] = {}
        self.assertEqual(run_checks(self.data["report"], self.data["schema"], self.data["plan"], self.data["fixtures"])["status"], "failed")

    def test_integer_literals_outside_backend_range_are_rejected(self):
        self.data["plan"]["columns"][2]["expression"] = {"literal": 2 ** 100}
        with self.assertRaisesRegex(InvalidInput, "64-bit"):
            self.compile()

    def test_null_first_coalesce_still_checks_all_operand_types(self):
        self.data["plan"]["columns"][1]["expression"] = {"op": "coalesce", "args": [{"literal": None}, {"literal": "text"}, {"literal": 1}]}
        with self.assertRaisesRegex(InvalidInput, "compatible"):
            self.compile()

    def test_malformed_expected_rows_report_failure_without_crashing(self):
        self.data["fixtures"]["cases"] = [{"name": "malformed", "parameters": {"workspace": "demo-b"}, "expected_rows": ["not an object"]}]
        outcome = run_checks(self.data["report"], self.data["schema"], self.data["plan"], self.data["fixtures"])
        self.assertEqual(outcome["status"], "failed")
        self.assertIn("objects", outcome["cases"][0]["reason"])


class FileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.project = self.base / "inputs"
        self.project.mkdir()
        for source in (ROOT / "examples").glob("*.json"):
            (self.project / source.name).write_bytes(source.read_bytes())
        self.config = self.project / "project.json"

    def tearDown(self):
        self.temp.cleanup()

    def test_two_roots_produce_identical_bytes_and_repeat_is_unchanged(self):
        one, state = build(self.config, self.base / "one")
        self.assertEqual(state, "created")
        repeated, state = build(self.config, self.base / "one")
        self.assertEqual((repeated, state), (one, "unchanged"))
        two, _ = build(self.config, self.base / "two")
        inventory = lambda folder: {p.name: p.read_bytes() for p in folder.iterdir()}
        self.assertEqual(inventory(one), inventory(two))
        self.assertTrue(verify(one, self.config)["reproduced_from_inputs"])

    def test_no_source_files_or_absolute_paths_are_copied(self):
        folder, _ = build(self.config, self.base / "output")
        self.assertEqual({p.name for p in folder.iterdir()}, {"query.sql", "standalone.sql", "definition.json", "review.json", "checks.json", "manifest.json"})
        self.assertNotIn(str(self.base), "\n".join(p.read_text(encoding="utf-8") for p in folder.iterdir()))

    def test_local_edits_are_detected_and_never_overwritten(self):
        folder, _ = build(self.config, self.base / "output")
        query = folder / "query.sql"
        query.write_bytes(query.read_bytes() + b"-- local edit\n")
        with self.assertRaisesRegex(InvalidInput, "not be overwritten"):
            build(self.config, self.base / "output")
        with self.assertRaisesRegex(InvalidInput, "hash"):
            verify(folder)

    def test_extra_or_missing_files_fail_verification(self):
        folder, _ = build(self.config, self.base / "output")
        (folder / "unexpected.txt").write_text("extra", encoding="utf-8")
        with self.assertRaisesRegex(InvalidInput, "inventory"):
            verify(folder)

    def test_failed_checks_create_no_output_directory(self):
        path = self.project / "fixtures.json"
        fixture, _ = read_json(path)
        fixture["cases"][0]["expected_count"] = 1000
        path.write_bytes(json_bytes(fixture))
        with self.assertRaisesRegex(InvalidInput, "no bundle"):
            build(self.config, self.base / "output")
        self.assertFalse((self.base / "output").exists())

    def test_reproduction_detects_input_changes(self):
        folder, _ = build(self.config, self.base / "output")
        path = self.project / "report.json"
        report, _ = read_json(path)
        report["title"] = "Changed synthetic title"
        path.write_bytes(json_bytes(report))
        with self.assertRaisesRegex(InvalidInput, "reproduce"):
            verify(folder, self.config)

    def test_parent_traversal_in_project_members_is_rejected(self):
        project, _ = read_json(self.config)
        project["report"] = "../private.json"
        self.config.write_bytes(json_bytes(project))
        with self.assertRaises(InvalidInput):
            load_project(self.config)

    def test_symlinked_input_and_output_parents_are_rejected(self):
        external = self.base / "external"
        external.mkdir()
        (external / "input.json").write_bytes((self.project / "report.json").read_bytes())
        (self.project / "link.json").symlink_to(external / "input.json")
        project, _ = read_json(self.config)
        project["report"] = "link.json"
        self.config.write_bytes(json_bytes(project))
        with self.assertRaises(InvalidInput):
            load_project(self.config)

    def test_initializer_refuses_to_replace_existing_files(self):
        path = self.base / "plan.json"
        write_new(path, b"original")
        with self.assertRaises(FileExistsError):
            write_new(path, b"replacement")
        self.assertEqual(path.read_bytes(), b"original")

    def test_duplicate_json_properties_and_non_finite_values_are_rejected(self):
        path = self.base / "bad.json"
        for raw in (b'{"x":1,"x":2}', b'{"x":NaN}'):
            path.write_bytes(raw)
            with self.assertRaises(InvalidInput):
                read_json(path)

    def test_cli_build_verify_and_init(self):
        output = self.base / "output"
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            self.assertEqual(main(["build", "--project", str(self.config), "--out", str(output)]), 0)
        folder = Path(json.loads(stream.getvalue())["bundle"])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["verify", str(folder), "--project", str(self.config)]), 0)
            self.assertEqual(main(["init", "--report", str(self.project / "report.json"), "--output", str(self.base / "new-plan.json")]), 0)

    def test_audit_detects_secret_patterns_without_printing_values(self):
        value = "example-" + "only-value"
        content = json.dumps({"pass" + "word": value})
        (self.base / "config.json").write_text(content, encoding="utf-8")
        outcome = audit_tree(self.base)
        self.assertEqual(outcome["status"], "review_required")
        self.assertNotIn(value, json.dumps(outcome))


if __name__ == "__main__":
    unittest.main()
