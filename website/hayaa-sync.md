# Hayaa Community Sync

**Scan once, help everyone.** Scanning a film takes hours of CPU; with Hayaa, the first server to scan a file shares what it found, and every other server with the same file gets those segments, confirmed by more users with each sync. Hayaa is new and its library is small, so every server that joins and shares its scans makes it better for everyone.

Turn it on under **Settings → Community Sync (Hayaa) → Sign in to Hayaa**. It's free, and you can unlink at any time.

## Privacy and what is shared

Hayaa is a shared segment service: servers share what their scans found, and what their users confirmed or rejected, so a title scanned once helps everyone with the same file.

**What is shared:**

- **File matching descriptors:** a content key (a hash computed from the file size and 16 sampled 64 KB chunks—about 1 MB total, without uploading the video), a hashed release key (from file name and size, without the readable name), and the runtime.
- **Scan findings:** segment time ranges, detector family (`nudenet`, `siglip`, or `subtitles`), labels, and confidence—or a note that a full scan found nothing (`clean`).
- **Your feedback and verification results:** confirming a kept segment, rejecting a false positive, relabelling a segment, withdrawing support when you delete your own segment, and local re-check outcomes.

**What never leaves your server:**

- Titles, readable file names or paths, Plex tokens, server addresses, usernames, or email addresses.
- Thumbnails or any video frames.
- Matched subtitle words (shared subtitle segments carry category `language` and severity only, never the words).
- **Segments from imported skip files** (third-party data is never shared).
- Your personal preferences: category levels, thresholds, and ignore choices.

Only titles made of a single file are synced.

## Signing in

1. Open **Settings → Community Sync (Hayaa) → Sign in to Hayaa**.
2. Hayaa for Plex displays a short code and link.
3. Open the link on any phone or computer, sign in with a passkey or an emailed code, confirm your Plex account once (it must own a Plex server and be at least 90 days old; one Hayaa account per Plex account and per server), and confirm the code.
4. New accounts beyond Hayaa's daily limit join a waitlist and get in on a later day by signing in again.

Hayaa for Plex registers its own signing key with your account. The key and sign-in token stay in `hayaa/` in the data directory (excluded from database backups), never in the database. **Unlink** revokes this server immediately; you can also unlink it from the Hayaa account page.

## Syncing

**Sync with Hayaa** sends what changed since the last sync and fetches updated titles. Turn on **Sync automatically** to do this every few hours (3 to 48); automatic syncs use at most four fifths of Hayaa's daily quota, so a manual sync always fits.

When Hayaa asks to wait (busy or over quota), syncing pauses until the time it gives.

## Deleted files

A finished scan remembers the file's fingerprint, so you can delete a file you only downloaded to scan and its scan is still shared on the next sync. Such titles are upload-only: nothing comes back for a file you no longer have. Files deleted before this change have no fingerprint and cannot be shared.

## What you get back, and when

Hayaa guards its data against people who would use it to cut scenes out of films:

- It returns segments only for files this server has scanned itself, after Hayaa has checked that the file is really here (Hayaa for Plex answers by reading a few small parts of the file).
- Segments are returned only once your account is **14 days old with 25 titles scanned**.
- Each day's titles count against a viewing allowance (about 8 hours of runtime a day), long films arrive a part at a time at about playback speed, and times are rounded outward by a few seconds.
- New accounts (first 7 days) also have pushes spread across syncs at a tenth of the normal quota.

The sync summary says how many titles were held back and why; they come on later syncs.

## What your actions share

- Marking a segment a **false positive** is a vote that nothing is there; to stop skipping a kind of scene, lower the viewer's category level instead (nothing is shared).
- **Deleting your own segment** withdraws your support; a **relabel** is a label vote.
- Deleting a segment that came from Hayaa only hides it here, for good.

## How shared segments are applied

Nothing from Hayaa acts until it passes these checks:

| Preset | Applies |
|---|---|
| **Balanced** (default) | Segments other users agree on; unconfirmed ones wait for a re-check on this server |
| **Cautious** | Also unconfirmed segments; ignores segments the community rejected |
| **Verify first** | Only what this server re-checked and found itself |

Re-checks run in the scan window and look only at the shared segment's few seconds, with your own detectors and thresholds. Shared segments that only a detector found also have to pass your thresholds.

- A segment of yours the community rejected is hidden (low and medium severity) and listed under **Community false positives**, with **Restore**.
- Unconfirmed ones wait under **Waiting for review**: **Keep** applies one and vouches for it, **Dismiss** hides it for good.
- If this server's re-checks keep disagreeing with shared segments, the preset switches itself to **Verify first** and says so.
