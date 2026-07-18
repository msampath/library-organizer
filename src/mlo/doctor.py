"""mlo doctor (P21/C5): one command a support flow can ask for — version,
resolved config summary, root/staging reachability INCLUDING writability
(closes G349: `config.validate` checks library/source existence but never
touches staging), store health (journal position, pending crash-recovery
rows, stale artifacts), LLM chain preflight (reused from `mlo check`), and
the last run's outcome.

Read-only: this module owns no mutation. The writability check is
`os.access(path, os.W_OK)` — a permission query, never an actual write (only
safeops.py may touch the filesystem, L0/L18)."""
from __future__ import annotations

import os

from . import __version__, winpath
from .agent.llm import preflight as llm_preflight
from .config import Config
from .store import Store


def _root_status(path: str) -> str:
    """MISSING / READ-ONLY / ok. Windows caveat: os.access(W_OK) reflects
    only the read-only attribute, not NTFS/share ACLs — an ACL-denied root
    can still report 'ok' here; the kernel's execute-time failure (journaled,
    fail-safe) is the backstop (super-review A-056)."""
    lp = winpath.to_long(path)
    if not os.path.isdir(lp):
        return "MISSING"
    if not os.access(lp, os.W_OK):
        return "READ-ONLY"
    return "ok"


def report(cfg: Config, store: Store) -> dict:
    """A structured health report. The CLI renders it; other callers (the
    web UI, scripts) can consume the dict directly."""
    sources = [
        {"name": s.name, "root": s.root, "enabled": s.enabled,
         "status": _root_status(s.root) if s.enabled else "disabled"}
        for s in cfg.sources]
    staging = [{"key": key, "root": root, "status": _root_status(root)}
              for key, root in sorted(cfg.staging.items())]

    pending = len(store.pending_ops())
    stale = [a.artifact_id for a in store.artifacts_all() if a.status == "stale"]

    llm_chain = None
    if cfg.llm.enabled and cfg.llm.chain:
        llm_chain = [{"entry": r.entry, "ok": r.ok, "detail": r.detail}
                    for r in llm_preflight(cfg)]

    last_run = store.last_run()

    return {
        "version": __version__,
        "config_path": cfg.path,
        "config_hash": cfg.config_hash,
        "library": {"root": cfg.library_root,
                   "status": _root_status(cfg.library_root)},
        "sources": sources,
        "staging": staging,
        "store": {"path": os.path.join(store.workspace, "state.db"),
                  "journal_pos": store.journal_pos(),
                  "pending_ops": pending,
                  "stale_artifacts": stale},
        "llm_chain": llm_chain,
        "last_run": last_run,
    }
