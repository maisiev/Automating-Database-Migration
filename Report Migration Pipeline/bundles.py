"""Deterministic output bundles. Inputs are hashed, never copied into exports."""

import os
from pathlib import Path

from . import __version__
from .checks import run_checks
from .compiler import compile_report
from .contracts import InvalidInput, digest, json_bytes, load_project, read_json


ARTIFACTS = {"query.sql", "standalone.sql", "definition.json", "review.json", "checks.json"}


def assemble(project):
    data = project["inputs"]
    compiled = compile_report(data["report"], data["schema"], data["plan"], project["dialect"])
    checks = run_checks(data["report"], data["schema"], data["plan"], data.get("fixtures"))
    if checks["status"] == "failed":
        failed = [r["name"] for r in checks["cases"] if r["status"] == "failed"]
        raise InvalidInput("Fixture checks failed; no bundle was created. Cases: " + ", ".join(failed))
    files = {
        "query.sql": compiled["query"].encode("utf-8"),
        "standalone.sql": compiled["standalone"].encode("utf-8"),
        "definition.json": json_bytes(compiled["definition"]),
        "review.json": json_bytes(compiled["review"]),
        "checks.json": json_bytes(checks),
    }
    manifest = {"format_version": 1, "generator_version": __version__, "report_id": data["report"]["id"], "dialect": project["dialect"], "input_hashes": project["input_hashes"], "files": {k: digest(v) for k, v in sorted(files.items())}}
    fingerprint = digest(json_bytes(manifest))
    manifest["fingerprint"] = fingerprint
    files["manifest.json"] = json_bytes(manifest)
    return files, fingerprint


def write_new(path, raw):
    path = Path(path)
    if path.is_symlink() or any(p.is_symlink() for p in path.parents):
        # /tmp may itself be a platform alias; callers resolve trusted roots first.
        raise InvalidInput("New-file destinations must not contain symlinks.")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)


def _inventory(folder):
    if folder.is_symlink() or any(p.is_symlink() for p in folder.rglob("*")):
        raise InvalidInput("Bundles cannot contain symlinks.")
    return {p.relative_to(folder).as_posix(): p.read_bytes() for p in folder.rglob("*") if p.is_file()}


def build(project_file, output_root):
    project = load_project(project_file)
    files, fingerprint = assemble(project)
    root = Path(output_root).resolve()
    for source in project["paths"]:
        if root == source.parent or root in source.parents:
            raise InvalidInput("Output roots cannot contain the input files. Use a separate artifact directory.")
    folder = root / project["inputs"]["report"]["id"] / fingerprint[:16]
    if folder.parent.is_symlink() or folder.parent.exists() and root not in folder.parent.resolve().parents:
        raise InvalidInput("Output directories cannot redirect outside the selected root.")
    if folder.exists():
        if _inventory(folder) != files:
            raise InvalidInput("An existing bundle was edited or is incomplete; it will not be overwritten.")
        return folder, "unchanged"
    folder.parent.mkdir(parents=True, exist_ok=True)
    folder.mkdir()
    # Publish the manifest last; an interrupted bundle is never considered valid.
    for name in sorted(files, key=lambda value: (value == "manifest.json", value)):
        write_new(folder / name, files[name])
    return folder, "created"


def verify(folder, project_file=None):
    folder = Path(folder).resolve()
    actual = _inventory(folder)
    if set(actual) != ARTIFACTS | {"manifest.json"}:
        raise InvalidInput("Bundle inventory is incomplete or contains unexpected files.")
    manifest, _ = read_json(folder / "manifest.json")
    if set(manifest) != {"format_version", "generator_version", "report_id", "dialect", "input_hashes", "files", "fingerprint"} or manifest["format_version"] != 1:
        raise InvalidInput("Unsupported manifest format.")
    if set(manifest["files"]) != ARTIFACTS:
        raise InvalidInput("Manifest file paths are not the expected fixed artifact set.")
    if any(digest(actual[name]) != sha for name, sha in manifest["files"].items()):
        raise InvalidInput("An artifact no longer matches its recorded hash.")
    core = {k: v for k, v in manifest.items() if k != "fingerprint"}
    if digest(json_bytes(core)) != manifest["fingerprint"]:
        raise InvalidInput("Manifest fingerprint does not match.")
    reproduced = False
    if project_file is not None:
        if manifest["generator_version"] != __version__:
            raise InvalidInput("Use the recorded generator version to reproduce this bundle.")
        expected, _ = assemble(load_project(project_file))
        if expected != actual:
            raise InvalidInput("Current inputs do not reproduce this bundle.")
        reproduced = True
    return {"status": "verified", "report_id": manifest["report_id"], "reproduced_from_inputs": reproduced, "business_approval": "pending"}
