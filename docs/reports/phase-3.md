BLOCKED — the expanded live smoke passed; ten independent expanded-data sessions and a held-out level comparison remain unavailable.

# Phase 3 checkpoint

Branch: `codex/decision-engine-improvements`. No PR, per the user's direction.

The November 11, 2026 Public.com probe supports multiple expirations, day-period SPX/NDX one-minute bars, and batched option quote polling. The isolated live workspace enabled those observational collectors and chain-native Greeks/quotes with a two-request-per-second cap. The original app's runtime directory was not used. Existing 0DTE snapshot columns, broadcaster fields, and the displayed decision remain unchanged.

## Live smoke, 13:09:29–13:39:29 ET

| Measure | Observed |
| --- | ---: |
| Collector cycles | 21 successful; 0 failed |
| Cycle duration | 23.97 s minimum, 24.15 s median, 24.65 s maximum |
| 0DTE snapshots | 105 across five symbols |
| 0DTE contract rows per snapshot | 36 minimum, 156.6 mean, 405 maximum; 16,441 total |
| Expiration contract rows per snapshot | 928 minimum, 2,595 mean, 4,555 maximum; 272,471 total |
| Estimated-flow poll rows | 530; mean classified-volume coverage 83.7% |
| Persisted index bars | 273 each for SPX and NDX at the end of the window |
| Collector log | 0 HTTP 429 responses; 0 errors; 0 warnings in the measured window |

An initial chain-native cycle failed because the installed SDK returns a `GreekValues` object, while the collector expected a dictionary. The collector was reverted immediately, fixed with an attribute-object regression test, and restarted at 13:09:29 ET. The clean smoke window begins after that correction. A second live finding fixed term-chain strike extraction from `option_details`; all five symbols now store six expirations per snapshot. Retention now removes expired term contracts, term snapshots, index bars, flow polls, and daily OI alongside old 0DTE snapshots.

The index bar labeler uses 60-second paths for SPX/NDX outcomes where bars exist; other symbols retain their recorded snapshot-path resolution. Combined and mapped levels remain observational. `scripts/validate-term-levels.py` has produced observations but has no held-out expanded-data session yet, so no new level source is eligible to feed version 2.

The main SQLite database grew from 113,840,128 bytes at 13:25:51 ET to 160,145,408 bytes at 13:56:11 ET: 46,305,280 bytes over 30.33 minutes, or 87.37 MiB/hour at that rate. The end reading also had a 7,506,672-byte WAL file; the baseline WAL size was not recorded, so the hourly figure covers the main database file only. That interval added 105 snapshots and 272,499 term contracts.

Verification after the live fixes: `npm run build` passed, `npm test` 85 passed, `npm run test:e2e` 17 passed on a separate demo port, `python -m pytest backend/tests -q` 234 passed, `npm run native:test` 4 passed, native release and installer built, packaged demo UI and frozen live/demo sidecar smoke passed.

## Native live launch

The browser development process released port 5010 after the smoke run. Before native launch, this app's schema-v3 desktop database (34,385 snapshots) was backed up to `gex_data.pre-expansion-2026-11-11.db` in the same isolated app-data directory. The native live UI smoke passed, and the rebuilt desktop app migrated its live database to schema v10 and started its Collector and NinjaTrader bridge on port 5010.

The first native collection saved all five symbols but retention reported a malformed SQLite index. Full `PRAGMA integrity_check` on the pre-migration backup showed missing index entries, establishing that the index issue predates migration. After closing only this app, `REINDEX` rebuilt the live database indexes and full integrity checking returned `ok`. The app was relaunched; its next cycle saved all five symbols, stored six expirations per symbol, and completed 30-day retention without the warning. The live database then contained 10,060 retained snapshots; the pre-migration backup remains available for historical replay. No original-app runtime directory was read or modified.
