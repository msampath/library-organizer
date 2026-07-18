"""CI LAW (defect L0/L18): no module but the kernel touches the filesystem.

Walks the AST of every module under src/mlo and fails on any filesystem-mutating
construct outside an explicit per-file whitelist. This is what makes the kernel
boundary structural rather than disciplinary. If this test is red, the change is
wrong — do not widen the whitelist without a defect-ledger discussion.
"""
from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "mlo"

# os.<name> / bare <name> (via from-import) that mutate the filesystem
OS_MUTATORS = {
    "rename", "renames", "replace", "remove", "unlink", "rmdir", "removedirs",
    "makedirs", "mkdir", "truncate", "link", "symlink", "system",
}
# attribute methods that mutate regardless of receiver (pathlib etc.)
ATTR_MUTATORS = {
    "write_text", "write_bytes", "unlink", "rmdir", "rmtree", "touch",
    "rename", "replace", "mkdir",
}
BANNED_IMPORTS = {"shutil", "subprocess"}

# file -> set of allowances
WHITELIST: dict[str, set[str]] = {
    "safeops.py": {"os-mutators", "open-write", "os.open"},
    "store.py": {"os.makedirs"},                          # workspace dir creation only
    "report.py": {"open-write", "csv", "os.makedirs"},    # exported views + run dirs
    "plan.py": set(),                     # plans are written via report helpers
}


def _mode_is_write(call: ast.Call) -> bool:
    mode = None
    if len(call.args) >= 2 and isinstance(call.args[1], ast.Constant):
        mode = call.args[1].value
    for kw in call.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
            mode = kw.value.value
    return isinstance(mode, str) and any(c in mode for c in "wax+")


def violations_for(path: Path) -> list[str]:
    allowed = WHITELIST.get(path.name, set())
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                base = a.name.split(".")[0]
                if base in BANNED_IMPORTS:
                    out.append(f"{path.name}:{node.lineno} import {a.name}")
                if base == "csv" and "csv" not in allowed:
                    out.append(f"{path.name}:{node.lineno} import csv "
                               f"(engine never reads CSVs; views live in report.py)")
        elif isinstance(node, ast.ImportFrom):
            base = (node.module or "").split(".")[0]
            if base in BANNED_IMPORTS:
                out.append(f"{path.name}:{node.lineno} from {node.module} import ...")
            if base == "os":
                for a in node.names:
                    if a.name in OS_MUTATORS and "os-mutators" not in allowed:
                        out.append(f"{path.name}:{node.lineno} from os import {a.name}")
            if base == "csv" and "csv" not in allowed:
                out.append(f"{path.name}:{node.lineno} from csv import ...")
        elif isinstance(node, ast.Call):
            f = node.func
            # os.<mutator>(...) and os.path-level mutators
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) \
                    and f.value.id == "os":
                if f.attr in OS_MUTATORS and "os-mutators" not in allowed:
                    if not (f.attr == "makedirs" and "os.makedirs" in allowed):
                        out.append(f"{path.name}:{node.lineno} os.{f.attr}()")
                if f.attr == "open" and "os.open" not in allowed:
                    out.append(f"{path.name}:{node.lineno} os.open()")
            # <anything>.<attr-mutator>(...)
            elif isinstance(f, ast.Attribute) and f.attr in ATTR_MUTATORS:
                if f.attr == "replace" and not (
                        len(node.args) == 1 and not node.keywords):
                    pass    # str.replace(a, b) / dataclasses.replace(x, k=v)
                            # — only Path.replace(target) has the 1-arg shape
                elif "os-mutators" not in allowed:
                    out.append(f"{path.name}:{node.lineno} .{f.attr}()")
            # open(..., 'w'/'a'/'x')
            elif isinstance(f, ast.Name) and f.id == "open":
                if _mode_is_write(node) and "open-write" not in allowed:
                    out.append(f"{path.name}:{node.lineno} open(mode=write)")
    return out


def test_kernel_is_the_only_door():
    assert SRC.is_dir(), f"missing {SRC}"
    problems: list[str] = []
    for py in sorted(SRC.rglob("*.py")):
        problems.extend(violations_for(py))
    assert not problems, (
        "Filesystem mutation outside the kernel (see docs/defect-ledger.md L0/L18):\n"
        + "\n".join(problems))


def test_shfileoperation_only_in_safeops():
    """P21/C2 — the L18 amendment, documented and enforced: the ONE ctypes
    call this codebase makes to touch the filesystem (the Windows Recycle
    Bin, FOF_ALLOWUNDO — never a delete) is allowed ONLY inside safeops.py,
    the sole kernel. Nothing else may reference it."""
    offenders = []
    for py in sorted(SRC.rglob("*.py")):
        if py.name == "safeops.py":
            continue
        if "SHFileOperationW" in py.read_text(encoding="utf-8"):
            offenders.append(py.name)
    assert not offenders, (
        "SHFileOperationW referenced outside safeops.py (the L18 amendment "
        "scopes it to the kernel only): " + ", ".join(offenders))


def test_no_deletion_primitives_anywhere():
    """Even the kernel gets no delete: rmtree/remove/unlink must appear nowhere."""
    banned = {"rmtree", "remove", "unlink", "removedirs"}
    problems = []
    for py in sorted(SRC.rglob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                f = node.func
                name = f.attr if isinstance(f, ast.Attribute) else (
                    f.id if isinstance(f, ast.Name) else None)
                if name in banned:
                    problems.append(f"{py.name}:{node.lineno} {name}()")
    assert not problems, "Deletion primitive found:\n" + "\n".join(problems)


def test_no_embedded_candidate_tables():
    """Defect L6: protected paths live in config, never as code literals. The
    walkers must derive pruning from cfg.protected_substrings — a hardcoded
    'bluestacks' would ignore a user's config and could leave a same-named
    library folder unindexed (duplicating content)."""
    # report.py is exempt: it authors the *example* mlo.toml, whose whole job is
    # to show the user the protected-substrings setting. Every other module must
    # take protected paths from config, never a literal.
    offenders = []
    for py in sorted(SRC.rglob("*.py")):
        if py.name == "report.py":
            continue
        for i, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if "bluestacks" in code.lower():
                offenders.append(f"{py.name}:{i}")
    assert not offenders, (
        "hardcoded protected-path literal (use cfg.protected_substrings):\n"
        + "\n".join(offenders))


def test_engine_never_reads_csv():
    """Ledger L7: CSVs are exported views. csv module allowed only in report.py."""
    for py in sorted(SRC.rglob("*.py")):
        if py.name == "report.py":
            continue
        src = py.read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                         else [node.module or ""])
                for n in names:
                    assert n.split(".")[0] != "csv", \
                        f"{py.name}:{node.lineno} imports csv outside report.py"
