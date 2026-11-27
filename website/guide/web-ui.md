# Web UI Pages

The web interface runs at `http://your-server:7979` and updates live over Server-Sent Events (SSE), so scans, new segments, synced titles, skips, active streams, and user or marker changes appear without a manual refresh.

## Dashboard

- **Active streams** — Shows all users currently watching, with playback position and controllability status
- **Scanner status** — Number of workers active, queue size, and which titles are currently being scanned
- **Recent skips** — Every recorded skip and mute (paged, with a total), with optional from/to date filters; failed actions are flagged. History is kept for `skip_event_retention_days`

## Browse

Titles, segments and Plex markers share one page. The sidebar lists your libraries; the main list always shows what is inside the current level (libraries, shows, seasons, movies, episodes) in the same rows, and clicking a row opens it. *Titles / Segments / Markers* choose what is listed for that level, and a movie or episode opens on its own page with its segments and markers. The level and view are part of the address, so a level can be bookmarked; the old `/library`, `/segments` and `/markers` addresses open Browse.

- **Search and filters** — The search box is always in the toolbar; *Filters* opens the rest below it, and active filters show as chips you can remove. While a filter is active the list shows the matches directly (a show whose name matches as one row, episodes with their show and season beside them). Filters: name, **actors** (the cast and the actors' photos are copied from Plex in the background; search the list, order it by most titles, name or most segments, and pick several actors, matching titles with any or all of them), rating, release-year range, scan-date range, scan status and ignored titles. The numbers in the tree count the titles that pass them
- **Sort** — Date added, scan date, name, release year or segments, with the direction spelled out (*Newest first / Oldest first*); defaults to date added, newest first
- **Segments at any level** — The Segments view lists every segment of the matching titles as rows with a 240 px frame (hover plays the preview, click opens the player), with segment filters in the same panel: state (active, marked false positive, rejected by the verifier), what found it, and label. Import sits behind *Import skip files* on a title's page
- **Bulk actions** — Tick titles, shows or seasons (or *select all filtered*) to scan, ignore, un-ignore or delete the segments of the selection; deleting segments of a whole title, show or selection asks first
- **Import skip files** — Upload `.skp` / `.edl` / `.mcf`, or paste a timestamp list. There is no export
- **Browse all titles** with scan status icons
- **Trigger scans** per title or entire library
- **"Scan Now"** to prioritize a title — moves it to the front of the scanning queue for immediate processing
- **Scan completion timestamps** — Shows when each title finished scanning

### Browse: segments

- **Live scanner banner** — Shows which titles are currently being scanned with progress bars
- **Segment details** — Timestamp range, confidence score, a thumbnail from the detected scene, and what found it (e.g. *NudeNet 640m*, *SigLIP*, *Subtitles*, *VideoSkip (.skp)*)
- **Delete false positives** — Remove incorrectly flagged segments
- **File cleanup** — Deleting a segment (or a title's segments, a rescan, or an import that replaces them) removes its thumbnail and hover preview. A sweep at startup and every 6 hours removes thumbnails, hover previews and feedback frames nothing references (older than an hour), and the saved data of removed titles (see *Removed titles* below). The preview clip and poster caches are pruned by age and size (see [Data, Backups & Maintenance](../reference/data-backups-maintenance.md)). Feedback frames kept for false-positive and relabel verdicts are never removed
- **Right-click to rescan** — On a movie, episode, season or show: *Rescan now* or *Rescan tonight* (queued for the scan window). On a show, season or library the first pair covers only its listed titles that have segments, so it rescans those listed episodes; *Rescan whole show now / tonight* and *whole season now / tonight* queue every non-ignored episode, including ones with no segments; right-clicking a library offers *Rescan whole library tonight* (finished and failed titles, scheduled only, never jumped to the front)
- **Failed scans** — At library level in Browse, *Retry failed* (the round-arrow button): every failed title of the library is queued again (inside your rating filter), except titles whose media file is still missing, which stay on the list and are counted; ignored titles are taken off the Failed list. The same choice is in Settings → *Rescan Titles*. A failed title shows the reason its scan failed. *Sync from Plex* also queues a failed title again when Plex reports a different file for it, when its file changed after the failure, or when the failure is more than a day old (a missing file stays failed)
- **Removed titles** — *Sync from Plex* drops titles Plex no longer reports. Their segments disappear from every list and count but are kept as marks; 30 days after removal everything else saved for the title (segment thumbnails and hover previews, cast, Plex markers, skip history, keyframe index) is deleted. A title that returns gets its marks back
- **Rescan many titles** — Settings → Scan Scope & Schedule → *Rescan Titles*: pick titles scanned by an older scanner (the right choice after an update), titles with segments, every scanned title, or titles scanned before a date, optionally for one library. A live count shows what will be queued and how many your rating filters skip; they go through the normal scan queue (scan window, usual order) and keep their current segments until the new scan replaces them
- **Live updates** — The open title's segments and the tree counts refresh as a rescan runs and when it finishes
- **Hover preview** — Hovering a segment's thumbnail plays a 5 s silent loop of it (sped up for long segments), in Browse and Analytics. Rendered overnight by the hover-preview job, so new segments get one the next night
- **Preview video** — In-app player; the server cuts a short H.264/AAC clip (5 s either side) so it plays in any browser, reading the file as the service account. *Open in VLC* starts and stops at the segment
- **Jump to segment** — Send Plex seek command to that spot in the video
- **Scan completion info** — Timestamp when the title finished scanning

### Browse: markers

- Lists the titles of a library with their **Plex intro and credits markers**, and lets you create, edit and delete them (and refresh them from Plex). A title's credits marker is what stops SigLIP from reading end credits as nudity

## Analytics

Tabs (linkable with `?tab=`), and sections that can be minimised (remembered per browser). The segment lists of *Detections* and *False positives* switch between a list and a **gallery** of large frames (remembered per browser):

- **Detections** — Segments per label (click a bar for a per-rating breakdown), a label filter, and the matching segments with preview, jump, false-positive and delete actions
- **Skips** — Skip and mute history by category, client and title
- **Accuracy** — How often each detector and label was marked a false positive by you (verifier rejections are counted separately and left out of the rate), with mean scores of kept vs. marked segments
- **False positives** — Every inactive segment, newest first, with an undo button: the ones you marked and the ones the verifier rejected (filter *Flagged by you / Rejected by the verifier*, each tagged)

## Users (under Settings)

- **Toggle filtering per Plex account** — Turn skipping on/off for specific user accounts
- **Category preferences** — Expand a user to set a `0–4` strictness level per content category, with an optional skip/mute override

## Settings

- **Plex Connection** — Server URL and token, with a connection test
- **Users** — Per-account filtering and category levels (above)
- **Playback & Enforcement** — Front door status and Plex settings, stream check interval, and the Seek before / Seek after buffers for fallback skipping
- **Scan Scope & Schedule** — Scan window, *Rescan Titles*, hover previews, scanner workers and hardware decoding, content-rating filter, library exclusions
- **NudeNet Detection** — Model (`320n` / `640m`), confidence threshold, scan frame interval, detector labels, per-label thresholds
- **SigLIP Fallback** — Enable it, threshold, per-label thresholds, consecutive frames, end-credits handling, the false-positive verifier
- **Segments** — Merge gap, refinement step (with how many older segments still wait for refinement), minimum hits
- **Hayaa** — Share segments with other servers through [Hayaa community sync](../hayaa-sync.md)
- Profanity wordlist and log level are stored as settings (`profanity_wordlist`, `log_level`)
