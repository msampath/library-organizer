# Classification patterns — distilled judgment for the classify-media task

This file records *why* the name patterns in `mlo/agent/tasks.py` (`NAME_PATTERNS`)
exist, so the knowledge survives the run that produced it. The design goal, set by a
real repair: **frontier-model judgment should be spent once, then distilled into
patterns and prompt rules that a small local model — or no model at all — can apply.**

## Provenance

On the first real library repair (388K files, 2026-07-06), 2,367 videos had no
derivable identity. A filename census against the fingerprint index showed **93%
(2,201) were definitional** — recorder and messenger naming conventions that need no
model. The remaining 108 went to a frontier model (Claude Opus, high reasoning
effort) with size and mtime context; its verdicts — 45 junk, 26 movie, 22 personal,
9 unsure, 6 music — were spot-verified and then distilled into the rules below.

## The deterministic layer (`NAME_PATTERNS`, first match wins)

| Rule | Kind | Convention it captures |
|---|---|---|
| `whatsapp-media` | personal | `VID-20200817-WA0012.mp4` — WhatsApp auto-saved media |
| `phone-timestamp` | personal | `20191221_161112.mp4` — Android/phone camera stamp |
| `dashcam-stamp` | personal | `20250520050008_0000001A.MP4` — dashcam timestamp counter |
| `dashcam-file` | personal | `FILE200817-092247F.mp4` — dashcam FILE-prefixed clips |
| `camera-prefix` | personal | `VID_`, `MOV_`, `MVI_`, `IMG_` + digits — consumer cameras |
| `nikon-dsc` | personal | `DSC_5731.MOV` — Nikon naming |
| `kodak-numbered` | personal | `112_0084.MOV` — Kodak folder_frame naming |
| `screen-recording` | personal | `Screen_Recording_2024...` — the user's own captures |
| `ios-export` | personal | `69455047495__7BA1AF15-....mp4` — iOS share/export naming |
| `ad-network-cache` | junk | `UnityAds-*` — ad SDK cache |
| `web-video-cache` | junk | `1444697903_570x320_low_quality.mp4` — epoch + resolution + quality suffix |
| `hex-named-cache` | junk | 32–40 hex chars + separator — content-addressed web cache |
| `temp-partial` | junk | `.temp-*` — interrupted download fragments |

Patterns match the **basename**, case-insensitively, before any LLM call. `junk`
never becomes a routing hint — the file stays put and is listed in a `*-junk.json`
sidecar for a later triage pass. Wrong-junk is therefore recoverable by design.

## The semantic layer (in the classify-media prompt)

Judgment the frontier run confirmed, now stated in the task prompt so any chain
model — including a local 20B — inherits it:

- **Device-vendor promo/tutorial videos are junk.** Backup drives ship marketing
  (`Introducing Seagate Backup Plus Video.mp4`, `... Dashboard Tutorial_4.mp4`);
  consolidation multiplies them (8 copies each here). Vendor literals belong in the
  user's `[classify.name_patterns]`, not in product defaults.
- **Game screen captures are junk**; generic `Screen_Recording_*` stays personal by
  default because the recorder convention outranks the guessed content.
- **Old FLV site rips are music**, not movies (`Artist%20-%20Song.flv`, `*Cd01.flv`).
- **DVD structures take identity from their folder.** `VTS_*.VOB` inside a named
  folder is classified (movie/music, language) but must carry `year = null` so it
  stays put — moving individual VOBs would shred the disc structure.
- **School events and named home recordings are personal** (`*_Drama_Club.mp4`,
  `Dance Demo - Toddlers.mp4`).
- **Generic names (`nice.mp4`, `supercute.mp4`) and pure-UUID phone files are
  `unsure`** — abstention beats a guess; unsure stays put.
- **Movie years are attested, never guessed**: a year is only emitted when the model
  is confident of the actual film's identity (`Quantum of Solace` → 2008). A movie
  without a year produces no move.

## Extending

```toml
[classify.name_patterns]           # consulted BEFORE the built-in defaults
junk = ['^Introducing Seagate ', '^Protecting Your Files With Seagate ']
```

Kinds allowed: `movie`, `tv`, `personal`, `music`, `junk`. Bad regexes and unknown
kinds fail config validation at startup (exit 2), not mid-classify.
