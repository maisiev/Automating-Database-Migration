"""A small local publication check, not a complete secret-detection service."""

import re
from pathlib import Path


SKIP = {".git", ".artifacts", "__pycache__", ".venv", "venv"}
RULES = {
    "private_key_marker": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "credential_assignment": re.compile(r"(?:password|passwd|client_secret|api_key|access_token)\s*[\"']?\s*[:=]\s*[\"'][^\"'\s]{8,}[\"']", re.I),
    "authenticated_url": re.compile(r"https?://[^\s/:@]+:[^\s/@]+@", re.I),
    "access_key_pattern": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "repository_token_pattern": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b"),
}


def audit_tree(root):
    root = Path(root).resolve()
    findings = []
    scanned = 0
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in SKIP for part in relative.parts):
            continue
        if path.is_symlink():
            findings.append({"file": relative.as_posix(), "rule": "symlink"})
            continue
        if not path.is_file():
            continue
        scanned += 1
        if path.stat().st_size > 4 * 1024 * 1024:
            findings.append({"file": relative.as_posix(), "rule": "oversized_file"})
            continue
        raw = path.read_bytes()
        try:
            content = raw.decode("utf-8-sig")
        except UnicodeError:
            findings.append({"file": relative.as_posix(), "rule": "binary_file_needs_manual_review"})
            continue
        for number, line in enumerate(content.splitlines(), 1):
            for name, pattern in RULES.items():
                if pattern.search(line):
                    findings.append({"file": relative.as_posix(), "line": number, "rule": name})
    return {"status": "passed" if not findings else "review_required", "files_scanned": scanned, "findings": findings, "limits": "Pattern checks do not prove absence of confidential data, establish publication rights, or guarantee security."}
