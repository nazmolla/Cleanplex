# Importing Skip Files

Hayaa for Plex reads skip files produced by other tools. **A title with an imported file is never ML-scanned**, so this is by far the cheapest way to fill your library.

## Supported formats

| Format | Extension | Notes |
|---|---|---|
| VideoSkip | `.skp` | Categories, 1–3 severity, and skip/mute/blank handling. The VideoSkip Exchange has ~563 movies and ~110 series, free and without an account |
| Kodi / MPlayer EDL | `.edl` | `start end action`; 0 cut, 1 mute, 2 scene marker, 3 commercial. |
| MovieContentFilter | `.mcf` | Spec 1.1.0, ~130 hierarchical categories. |
| Pasted list | `.txt` | Loose `00:12:30 - 00:13:05 nudity` lines, for lists that have no machine format |

## How to import

Two ways in:

- **Sidecar** — Drop `movie.skp` (or `.edl` / `.mcf`) next to `movie.mkv`. It is imported when the title is first discovered and the scan is skipped entirely.
- **Browse** — Open a title's *Segments* view and use the import box to upload a file or paste a list.

## Runtime check and export policy

Imported timings are authored against a specific cut of a film. Hayaa for Plex checks them against the title's runtime and warns when they do not line up, since an extended edition will throw every timestamp out.

Hayaa for Plex imports skip files but **never exports segments**, so its detections and synced segments cannot be redistributed as cutting lists. Imported segments are also never sent to [Hayaa community sync](../hayaa-sync.md).
