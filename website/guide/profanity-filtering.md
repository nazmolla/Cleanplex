# Profanity Filtering

Subtitles are scanned against a configurable wordlist and matching lines become **muted** segments — no frames, no inference, seconds per title.

## Word matching and audio tracks

Matching uses word boundaries with suffix handling, so `"shitting"` is caught while `"classic"` is not.

Because the timings come from one audio track, these segments are tagged with that track's language and only fire when it is the one playing; a dubbed track is left alone rather than muted in the wrong places.

To customize the wordlist, update **Profanity Wordlist** (`profanity_wordlist`), which accepts a JSON array of words. Leaving it empty uses the bundled list.

!!! note "Front door playback"
    A stream cannot be muted by editing an HLS playlist: in streams filtered by the [front door](../playback/front-door.md), profanity (`mute`) segments are either cut like a skip or left in, depending on **Mute segments in filtered streams** (`enforcement_mute_mode`: `cut` / `leave`, default `Leave`).

## Other content categories

Violence, gore, drugs and frightening content have **no local detector** — NudeNet covers nudity only. Those categories are populated by [importing skip files](importing-skip-files.md), where humans have already graded them.
