# Segment Expansion & Skip Logic

## Fallback skipping behavior

**Default behavior:** When a segment is detected (for example, `30s–60s`), Hayaa for Plex automatically:

1. **Widens the segment** by the Pre and Post Buffer settings → `27s–63s` at the `3000ms` defaults
2. **Monitors with a lookahead** of one poll interval — the filter triggers before the widened start to absorb polling latency
3. **Skips to 27s** and holds the block until playback reaches `63s`

Polling tightens automatically as a stream approaches a segment, so skips land accurately without polling Plex hard the rest of the time. Every skip is verified on the next tick: a client that accepts the command without actually moving is recorded as a failure and re-probed.

Streams edited by the [front door](../playback/front-door.md) do not use the pre/post buffers; they are cut at the exact segment edges.

## Categories, severity and actions

Segments carry a **category** (nudity, sex, violence, language, drugs, fear, commercial, …), a **severity** (`low`/`medium`/`high`) and an **action**:

| Action | Behaviour |
|---|---|
| `skip` | Seeks past the segment |
| `mute` | Drops the volume for the segment and restores it after — used for profanity |

Each user gets a `0–4` level per category (Off, Severe only, Moderate and up, Mild and up, Everything). A segment fires when *level + severity* exceeds `4` (`minimal` 1, `low` 2, `medium` 3, `high` 4), so level `4` filters everything in that category and level `0` filters nothing. Every category starts at level `4` when filtering is on; lowering or turning one off is always an explicit choice, and changing one category never affects the others.

**Scanner segments get their severity from the detected label**, so a level has a concrete meaning for nudity:

| Detected | Severity | Skipped at level |
|---|---|---|
| Exposed genitalia, exposed female breast | `high` | 1, 2, 3, 4 |
| Exposed buttocks, anus, belly | `medium` | 2, 3, 4 |
| Covered genitalia, covered female breast | `low` | 3, 4 |
| Everything else: other covered parts, feet, armpits, faces, male chest | `minimal` | 4 |

Imported skip files use MovieContentFilter's `low`, `medium` and `high`, which keep their order.

A segment with several labels takes the highest. Covered labels are not scanned by default; enable them under *Detector Labels* if level `3` should skip them. Imported segments keep the severity their author set. If the detector names the wrong thing, use the tag button on a segment to correct its labels: the severity follows, the correction is kept as feedback next to the detector's original labels, and a rescan of the same window keeps it.

`blank` and `blur` appear in some imported files but are **not supported**: Plex exposes playback position and volume, not the video stream. Those segments are logged and ignored rather than misapplied as skips.
