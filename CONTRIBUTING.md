# Contributing to mlo

Thanks for your interest. `mlo` is a safety-first engine for irreplaceable personal data,
so the bar for a change is higher than "it works" — it's "it cannot make the library
worse, and the tests prove it."

## The working agreement

Two documents are the contract; read them before a non-trivial change:

- **[docs/defect-ledger.md](docs/defect-ledger.md)** — every mechanism in this codebase
  traces to a real failure here. **Do not weaken a mechanism without consulting its entry.**
  If a change makes a ledger scenario possible again, the change is wrong.
- **[docs/architecture.md](docs/architecture.md)** — the safety-kernel boundary
  (only `safeops.py` mutates the filesystem; no delete/overwrite API exists anywhere) is
  enforced by `tests/test_architecture.py` and is non-negotiable.

## Ground rules

- **The kernel is the only door to the filesystem.** Never add a second mutation path, a
  delete, or an overwrite. `tests/test_architecture.py` fails CI if you do.
- **No data lists in code.** Sources, protected paths, junk rules, taxonomy, and layout
  live in `mlo.toml` (L6). Code reads the tables; it doesn't embed them.
- **A new behavior needs a test; a fixed defect needs a *named* regression test** cited in
  the defect ledger (`tests/test_defect_ledger.py` enforces that the citation is real).
- **Zero runtime dependencies.** The engine is stdlib-only (Python ≥ 3.11). The only
  extras are dev (`pytest`, `hypothesis`) and the opt-in enrichment connectors
  (`mlo[enrich]`: `mutagen`, `Pillow`, `thefuzz`) — the core never imports them at module load except behind a guarded try/except soft-import (`fuzzy.py`), degrading to stdlib behavior when absent.

## Development

```console
$ pip install -e ".[dev]"
$ pytest                 # the whole suite must pass, incl. the architecture + ledger tests
```

CI runs the suite on windows-latest and ubuntu-latest across Python 3.11 and 3.12. A change
is "done" when it is implemented, all tests pass, docs are in sync, and any owner-facing
decision has been surfaced — not when the code is merely written.
