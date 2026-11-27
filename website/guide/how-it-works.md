# How It Works

Hayaa for Plex separates heavy analysis from playback so that watching a film requires zero machine learning at playback time.

## 1. Pre-scan (background analysis)

New titles are queued and scanned inside the **scan window** (`23:00–06:00` by default), a few at a time:

- `ffmpeg` extracts a frame every **Scan Frame Interval** (`5 s` by default), using the machine's hardware video decoder when it has one
- Each frame goes through [NudeNet](https://github.com/notAI-tech/NudeNet), a local ONNX body-part detector (the bundled `320n` model, or the larger `640m`)
- Optionally, frames NudeNet passed over are judged by a second, whole-frame model, **SigLIP**, to catch scenes NudeNet misses (see [Detection Pipeline & Accuracy](../detection/pipeline-and-accuracy.md))
- Flagged frames are grouped into **segments**; each flagged window is then re-sampled every `500 ms` so the thumbnail is the strongest frame and the segment covers the whole scene
- A segment's severity comes from what was detected, and the optional **false-positive verifier** can reject detections that look like ones you have rejected before
- Titles with an [imported skip file](importing-skip-files.md) are never scanned, and subtitles are scanned for [profanity](profanity-filtering.md) without touching a frame
- Scan time depends on the frame interval, worker count and hardware decoding; everything runs locally, nothing is uploaded

## 2. Real-time playback monitoring

While someone watches, the service polls Plex (every `5 s` by default, tighter as a stream approaches a segment):

- Checks whether a filtered user's position is entering a flagged segment, widened by the **pre/post buffers** (`3 s` each by default)
- Sends a seek past the whole segment (or lowers the volume for a `mute` segment) and verifies on the next tick that the client really moved
- This is a database lookup; no ML runs at playback time

For clients that do not accept remote control commands (including Plex Web, iPhone, iPad, and Apple TV), the [front door](../playback/front-door.md) edits the stream directly before it reaches the player.

## 3. Web UI

A full browser interface runs at `http://your-server:7979`. Every page updates live: the server pushes changes (scans, new segments, synced titles, skips, active streams, user and marker changes) over Server-Sent Events, so nothing needs a manual refresh.

Pages include Dashboard, Browse (Library, Segments, Markers), Analytics, Users and Settings — see [Web UI Pages](web-ui.md).

## 4. Plex integration

Hayaa for Plex also writes a short **`"Cleanplex scan:"` block into each scanned title's Plex summary** (status, segment count and what found them, false positives and corrections). It is refreshed a couple of seconds after scans, deletes, false-positive marks, relabels and imports, and only touches its own block.
