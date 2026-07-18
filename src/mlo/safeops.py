"""THE SAFETY KERNEL. The only module in mlo allowed to mutate the filesystem.

Structural guarantees (see docs/architecture.md §3 and the defect ledger):
  - No delete, unlink, rmtree, or overwrite exists in this API — or anywhere
    else in the codebase (tests/test_architecture.py enforces by AST). L18.
  - PathPolicy is checked on BOTH ends of every operation. L12.
  - stage_move is same-drive and must land under that drive's staging root.
  - copy_in copies to a temporary '<dst>.mlopart', RE-HASHES it against the
    plan's fingerprint, then renames into place — a copy is never trusted
    unverified (L15) and a failure leaves only an inert .mlopart residue that
    verify reports.
  - Destination names are never generated here: an occupied destination is
    drift (skipped_drift), never a new name. L1, L17.
  - Dry-run and execute run the identical code path; `execute` only gates the
    syscalls and the journal writes. L9.
  - Completed ops are no-ops via the journal gate. L1.
  - Every terminal 'done' updates the library index in the same transaction
    (via Store.complete_op). L7.

Policy violations and drift RETURN statuses; they never raise. Raising is
reserved for programming errors (wrong arguments, unknown kinds).
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Callable

from . import fingerprint, staging, trash, winpath
from .store import Store

OP_KINDS = ("stage_move", "copy_in", "move_within", "rmdir_empty", "dispose")


def op_id_for(kind: str, src: str, dst: str,
              pre_size: int | None, pre_quick_hash: str | None) -> str:
    """Content-addressed operation identity, fixed at plan time (L1)."""
    payload = json.dumps({
        "kind": kind,
        "src": winpath.to_bytes(src).hex(),
        "dst": winpath.to_bytes(dst).hex(),
        "pre_size": pre_size,
        "pre_quick_hash": pre_quick_hash,
    }, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True)
class Blocked:
    reason: str


@dataclass(frozen=True)
class OpResult:
    op_id: str
    kind: str
    src: str
    dst: str
    status: str          # done | would_do | skipped_done | skipped_drift |
                         # skipped_protected | failed
    detail: str = ""

    @property
    def acted(self) -> bool:
        return self.status == "done"


class PathPolicy:
    """Protected paths + placement rules. drive_of is injectable for tests."""

    def __init__(self, protected_substrings: tuple[str, ...],
                 blocked_drives: tuple[str, ...],
                 staging_roots: dict[str, str],
                 library_root: str,
                 drive_of: Callable[[str], str] = winpath.drive_of):
        self.protected_substrings = tuple(s.lower() for s in protected_substrings)
        self.blocked_drives = tuple(d.upper() for d in blocked_drives)
        self.staging_roots = dict(staging_roots)
        self.library_root = library_root
        self.drive_of = drive_of

    def check(self, path: str) -> Blocked | None:
        plain = winpath.from_long(path)
        low = plain.lower()
        for sub in self.protected_substrings:
            if sub and sub in low:
                return Blocked(f"protected substring '{sub}' in {plain}")
        drive = self.drive_of(plain)
        if drive and drive in self.blocked_drives:
            return Blocked(f"protected drive {drive}: in {plain}")
        return None

    def staging_root_for(self, path: str) -> str | None:
        """P21/A4: resolves single-letter drive keys AND absolute-path-prefix
        keys (UNC shares, POSIX mounts) — see staging.root_for."""
        return staging.root_for(self.staging_roots, path, self.drive_of)


class SafeOps:
    def __init__(self, policy: PathPolicy, store: Store, run_id: str,
                 execute: bool, plan_id: str | None = None, disposer=None):
        self.policy = policy
        self.store = store
        self.run_id = run_id
        self.execute = execute
        self.plan_id = plan_id
        # Injectable like drive_of/transport elsewhere (P21/C2): the real OS
        # recycle/trash call is impractical and environment-dependent to
        # exercise in an automated test (a headless CI runner may have no
        # window station for SHFileOperationW; a real trash write pollutes
        # the actual OS trash) — tests inject a fake to verify the KERNEL's
        # placement/drift/journal logic, which is where the actual risk of
        # a defect lives. The real disposer is owner-verified on first live
        # use, per the plan's own gate.
        self.disposer = disposer or _default_disposer

    # ── public operations ────────────────────────────────────────────────

    def stage_move(self, src: str, dst: str, pre_size: int | None,
                   pre_quick_hash: str | None) -> OpResult:
        return self._run("stage_move", src, dst, pre_size, pre_quick_hash)

    def copy_in(self, src: str, dst: str, pre_size: int | None,
                pre_quick_hash: str | None) -> OpResult:
        return self._run("copy_in", src, dst, pre_size, pre_quick_hash)

    def move_within(self, src: str, dst: str, pre_size: int | None,
                    pre_quick_hash: str | None) -> OpResult:
        return self._run("move_within", src, dst, pre_size, pre_quick_hash)

    def rmdir_empty(self, path: str) -> OpResult:
        """The only removal in the codebase: os.rmdir, which atomically fails
        on non-empty directories. No fallback, recursive or otherwise (L18)."""
        return self._run("rmdir_empty", path, path, None, None)

    def dispose(self, path: str, pre_size: int | None,
               pre_quick_hash: str | None) -> OpResult:
        """P21/C2 — the L18 amendment: send a STAGING-ONLY file to the OS's
        own recycle bin / trash (Windows Recycle Bin via SHFileOperationW +
        FOF_ALLOWUNDO; POSIX XDG trash). This is not a delete primitive —
        every other module's ban on rmtree/remove/unlink is unchanged
        (test_no_deletion_primitives_anywhere still enforces it everywhere,
        including here); the file is recoverable through the OS's own UI.
        `_placement_error` refuses anything not under a configured staging
        root — dispose can never reach the library or a source."""
        return self._run("dispose", path, path, pre_size, pre_quick_hash)

    # ── the single code path (L9) ────────────────────────────────────────

    def _run(self, kind: str, src: str, dst: str,
             pre_size: int | None, pre_quick_hash: str | None) -> OpResult:
        if kind not in OP_KINDS:
            raise ValueError(f"unknown op kind {kind!r}")
        oid = op_id_for(kind, src, dst, pre_size, pre_quick_hash)

        def result(status: str, detail: str = "") -> OpResult:
            return OpResult(oid, kind, src, dst, status, detail)

        # 1. policy — BOTH ends (L12)
        for end, p in (("src", src), ("dst", dst)):
            blocked = self.policy.check(p)
            if blocked:
                return self._terminal(result("skipped_protected",
                                             f"{end}: {blocked.reason}"),
                                      journal=True)

        # 2. placement rules
        placement = self._placement_error(kind, src, dst)
        if placement:
            raise ValueError(placement)  # plans must never contain these

        # 3. journal gate (L1): ONLY a proven-done op is a no-op. A prior
        # drift/protected/failed skip is re-derivable, so it must be allowed to
        # retry once its cause is fixed — else residual plans could never make
        # progress and "complete but wrong" would be reachable (defect L5).
        if self.store.op_state(oid) == "done":
            return result("skipped_done", "journal: already done")

        # 4. preconditions on live disk (L9)
        drift = self._precondition_drift(kind, src, dst, pre_size, pre_quick_hash)
        if drift:
            return self._terminal(result("skipped_drift", drift), journal=True)

        # 5. rehearse or act
        if not self.execute:
            return result("would_do")
        return self._act(result, kind, src, dst, pre_size, pre_quick_hash)

    # ── helpers ──────────────────────────────────────────────────────────

    def _placement_error(self, kind: str, src: str, dst: str) -> str | None:
        if kind == "stage_move":
            sd, dd = self.policy.drive_of(src), self.policy.drive_of(dst)
            if not staging.same_volume(src, dst, self.policy.drive_of):
                return f"stage_move must be same-drive ({sd!r} -> {dd!r})"
            root = self.policy.staging_root_for(src)
            if not root:
                return f"no staging root configured for drive {sd!r}"
            if not winpath.is_under(dst, root):
                return f"stage_move dst must be under staging root {root}"
        elif kind == "copy_in":
            if not winpath.is_under(dst, self.policy.library_root):
                return "copy_in dst must be under the library root"
        elif kind == "move_within":
            lib = self.policy.library_root
            if not (winpath.is_under(src, lib) and winpath.is_under(dst, lib)):
                return "move_within must stay inside the library root"
        elif kind == "dispose":
            root = self.policy.staging_root_for(src)
            if not root or not winpath.is_under(src, root):
                return "dispose src must be under a configured staging root"
        return None

    def _precondition_drift(self, kind: str, src: str, dst: str,
                            pre_size: int | None,
                            pre_quick_hash: str | None) -> str | None:
        lsrc, ldst = winpath.to_long(src), winpath.to_long(dst)
        if kind == "rmdir_empty":
            if not os.path.isdir(lsrc):
                return "directory missing"
            return None
        if not os.path.exists(lsrc):
            return "source missing"
        if kind == "dispose" and not os.path.isfile(lsrc):
            # build_dispose only emits files, but the kernel API must not
            # trust its callers: a directory here would hand a whole tree
            # to the OS recycle/trash call.
            return "dispose source is not a regular file"
        if kind in ("stage_move", "copy_in", "move_within", "dispose"):
            # dispose journals dst == src by convention (a single-path op,
            # like rmdir_empty) — an "occupied destination" is meaningless
            # for it since dst IS the just-confirmed-present source.
            # lexists, not exists: a dangling symlink still occupies the
            # name, and POSIX os.rename would silently replace it.
            if kind != "dispose" and os.path.lexists(ldst):
                return "destination occupied"
            if pre_size is not None or pre_quick_hash is not None:
                try:
                    size, qh = fingerprint.quick(src)
                except OSError as e:
                    return f"source unreadable: {e}"
                if pre_size is not None and size != pre_size:
                    return f"size drift ({size} != planned {pre_size})"
                if pre_quick_hash is not None and qh != pre_quick_hash:
                    return "content drift (quick-hash mismatch)"
        return None

    def _act(self, result, kind: str, src: str, dst: str,
             pre_size: int | None, pre_quick_hash: str | None) -> OpResult:
        oid = result("x").op_id
        self.store.journal_intent(self.run_id, self.plan_id, oid, kind, src, dst,
                                  pre_size, pre_quick_hash)
        try:
            effect = self._syscall(kind, src, dst, pre_size, pre_quick_hash)
        except _CopyVerifyError as e:
            self.store.complete_op(oid, "failed", str(e))
            return result("failed", str(e))
        except OSError as e:
            self.store.complete_op(oid, "failed", f"{type(e).__name__}: {e}")
            return result("failed", f"{type(e).__name__}: {e}")
        self.store.complete_op(oid, "done", index_effect=effect)
        return result("done")

    def _syscall(self, kind: str, src: str, dst: str,
                 pre_size: int | None, pre_quick_hash: str | None):
        """Perform the operation; return the library-index effect tuple."""
        lsrc, ldst = winpath.to_long(src), winpath.to_long(dst)
        lib = self.policy.library_root

        if kind == "rmdir_empty":
            os.rmdir(lsrc)  # atomically fails if non-empty — the whole point
            return None

        if kind == "dispose":
            self.disposer(lsrc, winpath.from_long(src))
            return None    # staging content is never library-indexed (L7 n/a)

        os.makedirs(os.path.dirname(ldst), exist_ok=True)

        if kind == "copy_in":
            part = ldst + ".mlopart"
            src_times_ns = _stat_times_ns(lsrc)   # captured BEFORE the copy (P21/A2)
            self._copy_stream(lsrc, part)
            size, qh = fingerprint.quick(part)
            if pre_size is not None and size != pre_size:
                raise _CopyVerifyError(
                    f"post-copy size mismatch ({size} != {pre_size}); "
                    f"residue at {winpath.from_long(part)}")
            if pre_quick_hash is not None and qh != pre_quick_hash:
                raise _CopyVerifyError(
                    f"post-copy hash mismatch; residue at {winpath.from_long(part)}")
            self._rename_no_overwrite(part, ldst)
            if src_times_ns is not None:
                try:
                    os.utime(ldst, ns=src_times_ns)   # preserve the source's mtime
                except OSError:
                    pass                              # best-effort; content is verified
            return ("insert", self._lib_rel(dst, lib), size, qh,
                    _mtime_ns(ldst), self.run_id)

        if kind in ("stage_move", "move_within"):
            self._rename_no_overwrite(lsrc, ldst)
            src_in_lib = winpath.is_under(src, lib)
            dst_in_lib = winpath.is_under(dst, lib)
            if src_in_lib and dst_in_lib:
                return ("move", self._lib_rel(src, lib), self._lib_rel(dst, lib))
            if dst_in_lib:                        # moved INTO the library
                return ("insert", self._lib_rel(dst, lib), pre_size or 0,
                        pre_quick_hash or "", _mtime_ns(ldst), self.run_id)
            if src_in_lib:                        # moved OUT of the library
                return ("delete", self._lib_rel(src, lib))
            return None

        raise ValueError(f"unknown op kind {kind!r}")  # pragma: no cover

    @staticmethod
    def _copy_stream(lsrc: str, ldst_part: str) -> None:
        # Streamed copy with an exclusive-create destination: cannot overwrite
        # anything, including a concurrent .mlopart.
        with open(lsrc, "rb") as fin, open(ldst_part, "xb") as fout:
            while True:
                block = fin.read(4 * 1024 * 1024)
                if not block:
                    break
                fout.write(block)
            fout.flush()
            os.fsync(fout.fileno())

    @staticmethod
    def _rename_no_overwrite(lsrc: str, ldst: str) -> None:
        """Rename that never overwrites and never leaves a wedge behind.

        Windows os.rename is natively no-clobber. On POSIX os.rename would
        replace an existing dst, so we guard with an exists() check under the
        documented single-mutator-during-apply assumption (architecture §13) —
        deliberately WITHOUT materializing an exclusive-create claim file, which
        the engine could never remove (no delete primitive) and which a failed
        rename would leave as a permanent zero-byte occupant (defect ledger
        minor). A cross-device move is refused up front so it cannot half-happen."""
        # lexists, not exists: a dangling symlink occupies the name too, and
        # POSIX os.rename would silently replace the symlink inode.
        if os.path.lexists(ldst):
            raise FileExistsError(f"destination occupied: {winpath.from_long(ldst)}")
        if not winpath.is_windows():
            try:
                if os.stat(lsrc).st_dev != os.stat(os.path.dirname(ldst)).st_dev:
                    raise OSError("cross-device move refused (would not be atomic)")
            except FileNotFoundError:
                pass
        os.rename(lsrc, ldst)

    @staticmethod
    def _lib_rel(path: str, library_root: str) -> str:
        return os.path.relpath(winpath.from_long(path),
                               winpath.from_long(library_root))

    def _terminal(self, res: OpResult, journal: bool) -> OpResult:
        """Record a non-acting terminal outcome. In dry-run nothing is written;
        in execute mode drift/protected outcomes are journaled so reports and
        audits see them (resolved state, not intended — L16)."""
        if self.execute and journal and self.store.op_state(res.op_id) is None:
            self.store.journal_intent(self.run_id, self.plan_id, res.op_id,
                                      res.kind, res.src, res.dst, None, None)
            self.store.complete_op(res.op_id, res.status, res.detail)
        return res


class _CopyVerifyError(Exception):
    pass


def _default_disposer(lpath: str, display_path: str) -> None:
    """The real OS-level dispose call, dispatched by platform. `lpath` is
    the \\\\?\\-long form; `display_path` is the plain form. Windows gets the
    PLAIN path: SHFileOperationW does not accept the \\\\?\\ namespace — with
    the prefixed form it returns 0x7C (DE_INVALIDFILES) and recycles nothing
    (empirically reproduced; super-review finding H1). Consequence: this API
    is MAX_PATH-bound, so a staging path over ~260 chars fails the op — it
    journals 'failed' and the file stays in staging untouched (honest,
    fail-safe; an IFileOperation COM port would lift the limit)."""
    if winpath.is_windows():
        _recycle_windows(winpath.from_long(lpath))
    else:
        _trash_posix(lpath, display_path)


def _recycle_windows(path: str) -> None:
    """Send `path` (PLAIN form — never \\\\?\\-prefixed, see _default_disposer)
    to the Windows Recycle Bin via SHFileOperationW with FOF_ALLOWUNDO — the
    whole reason for this API over any delete call: the file is recoverable
    through the OS's own UI, not gone. This is the ONE ctypes call this
    codebase makes to touch the filesystem; the no-deletion AST law is
    amended surgically to allow it here only
    (tests/test_architecture.py::test_shfileoperation_only_in_safeops, the
    L18 amendment, P21/C2)."""
    import ctypes

    class _SHFILEOPSTRUCTW(ctypes.Structure):
        if ctypes.sizeof(ctypes.c_void_p) == 4:
            _pack_ = 1          # shellapi.h packs this struct on 32-bit
        _fields_ = [
            ("hwnd", ctypes.c_void_p),
            ("wFunc", ctypes.c_uint),
            ("pFrom", ctypes.c_wchar_p),
            ("pTo", ctypes.c_wchar_p),
            ("fFlags", ctypes.c_uint16),
            ("fAnyOperationsAborted", ctypes.c_int),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", ctypes.c_wchar_p),
        ]

    FO_DELETE = 3
    FOF_ALLOWUNDO = 0x40
    FOF_NOCONFIRMATION = 0x10
    FOF_SILENT = 0x4
    FOF_NOERRORUI = 0x400

    # pFrom must be double-NUL-terminated per SHFileOperationW's contract;
    # ctypes' own c_wchar_p marshalling appends the second NUL.
    op = _SHFILEOPSTRUCTW(
        hwnd=None, wFunc=FO_DELETE, pFrom=path + "\0", pTo=None,
        fFlags=FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI,
        fAnyOperationsAborted=0, hNameMappings=None, lpszProgressTitle=None)
    rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if rc != 0 or op.fAnyOperationsAborted:
        raise OSError(f"SHFileOperationW failed (code {rc}, "
                      f"aborted={bool(op.fAnyOperationsAborted)})")


def _trash_posix(lpath: str, display_path: str) -> None:
    """Move `lpath` into the XDG trash (files/) and write its .trashinfo
    (info/). `trash.trash_dirs_for` always resolves a SAME-DEVICE trash
    directory, so this is a plain os.rename — never a cross-device copy,
    which would need a delete-the-original step this codebase doesn't have.

    Ordering follows the XDG spec: the exclusive .trashinfo create IS the
    atomic name claim (checked free in BOTH files/ and info/), and only then
    is the file renamed in. The old inverse order could rename the file and
    THEN fail the info write, leaving trashed content with no restore
    metadata; this way a failure between the two steps leaves only an orphan
    .trashinfo, which standard trash tools clean up."""
    files_dir, info_dir = trash.trash_dirs_for(lpath)
    os.makedirs(files_dir, exist_ok=True)
    os.makedirs(info_dir, exist_ok=True)
    name = trash.unique_trash_name(files_dir, info_dir, os.path.basename(lpath))
    with open(os.path.join(info_dir, name + ".trashinfo"), "x", encoding="utf-8") as f:
        f.write(trash.trashinfo(display_path))
    os.rename(lpath, os.path.join(files_dir, name))


def _mtime_ns(lpath: str) -> int:
    try:
        return os.stat(lpath).st_mtime_ns
    except OSError:
        return 0


def _stat_times_ns(lpath: str) -> tuple[int, int] | None:
    """(atime_ns, mtime_ns) for os.utime(ns=...), or None if unreadable —
    copy_in's mtime preservation is best-effort and never blocks the copy."""
    try:
        st = os.stat(lpath)
        return st.st_atime_ns, st.st_mtime_ns
    except OSError:
        return None
