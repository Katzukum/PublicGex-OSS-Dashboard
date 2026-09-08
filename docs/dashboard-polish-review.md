# Dashboard reliability and visual review

Completed September 8, 2026, within the three-hour limit.

## Restore point

The entire starting working tree was committed and pushed before implementation:
`90ee206`, branch `codex/gamma-sweep-checkpoint`.
Improvements are on the separate `codex/dashboard-polish` branch.
Local databases and credentials remain outside Git according to the existing ignore rules.

## Changes

- Fixed an observed Eel/WebSocket corruption bug: simultaneous large TRACE and status replies could interleave frames and disconnect the browser. Socket writes are serialized; native-thread frontend events are queued onto the Eel event loop.
- Bounded frontend requests, cleaned up Eel callbacks, retained retry timers after startup failures, and added an offline/retry state. Symbol changes cannot display an older instrument's response.
- Added bounded, expiring workspace and TRACE caches. Workspace snapshots are pinned consistently and cross-asset updates invalidate their cache.
- TRACE now uses the latest poll in each minute instead of summing repeated polls. Session history follows the selected date. Replay starts with a useful time window, numeric strike cells render properly, and profile/heatmap price axes align. Latest-snapshot labels distinguish the side profile from the browsed time range.
- Reduced hidden-view work, duplicate chart updates, and unnecessary resizing. Charts use a bundled ECharts distribution and no remote font dependency.
- Improved cockpit hierarchy, natural scrolling, chart contrast, both themes, settings labels, collapsible navigation/activity feed, journal validation, empty states, and saved One-Off profiles. Strike Matrix includes an explicit Jump to spot action.
- Missing decision targets and gamma crossings no longer become zero prices. An unavailable event calendar is explicitly unavailable rather than appearing clear.

## Verification

- Python: **90 passed**. Twelve existing SQLite datetime-adapter deprecation warnings remain.
- Frontend Node tests: **24 passed**.
- JavaScript syntax and Git whitespace checks passed.
- Real transport integration test: six overlapping replies, including three approximately 7 MB payloads, all parse correctly. The corresponding concurrent request failed before the transport fix.
- Visually inspected the running app at **1920 x 1080**, plus 1280 x 800 and 1024 x 768. Checked cockpit, TRACE historical-date selection and scrubbing, Edge Lab, Regime, Strike Matrix, One-Off empty/saved profiles, and Settings. Verified both themes, collapse/reopen, saved-chart navigation, and Jump to spot.
- Stopped the local backend, observed OFFLINE plus Retry now, restarted it, and successfully reconnected using the UI retry control.
- Independent harsh critic: **8/10, PASS**, after iteration. Remaining minor feedback: compact cockpit metrics have some excess whitespace and secondary captions could be larger.

## Measured performance

On a disposable 5.688 GiB copy of the local database, SPX, September 4 session, 12 workspace runs:

| Path | Time |
| --- | ---: |
| Cold workspace | 2,154 ms |
| Cached workspace median | 9.88 ms |
| Cached workspace p95 | 10.43 ms |
| Cold TRACE, 26,278 cells | 1,763 ms |
| Cached TRACE | 187 ms |

These are local backend timings, not end-to-end browser latency. Cold replay was approximately 2.1-2.3 seconds in earlier measurements; disk/cache state varies.

## Scope and evidence

Screenshots are in [ui-review](ui-review/). Files marked `final` show the concluding review; other files document dark theme, historical replay, compact layout, and backend failure.

Validation used existing historical market data. New live option-chain pulls and broker order execution were not exercised. The browser preview was restored at 1920 x 1080 with the original light-theme settings. Its backend serves saved data; normal collection starts through the application's usual launcher.


## Live polling follow-up

On September 8 at approximately 05:20 ET, started the collector and confirmed successive successful cycles for SPY, QQQ, IWM, SPX, and NDX. Each cycle made 32 requests and scheduled the next poll after 15 seconds (in addition to the roughly 25-second collection duration).

This exposed a separate live-data issue: missing quote fields became Pandas NaN values, which Eel serialized as invalid browser JSON. The transport now converts non-finite numbers to null without changing valid values or source payloads. Regression coverage includes strict JSON parsing and real WebSocket replies containing missing quotes. All 91 Python tests pass. The browser now renders current snapshots and shows FRESH; polling remains running. The user's dark-theme preference is saved locally.
