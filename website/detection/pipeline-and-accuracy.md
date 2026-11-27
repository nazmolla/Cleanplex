# Detection Pipeline & Accuracy

Hayaa for Plex combines fast body-part detection, an optional whole-frame fallback model, segment clustering and refinement, and an optional personal false-positive verifier.

## How detection works

**NudeNet** finds body parts and gives each a confidence. A frame is flagged when a label you enabled under *Detector Labels* reaches its threshold (the global *Confidence Threshold*, or a per-label override). Covered labels are not scanned by default.

**SigLIP fallback** (`off` by default; downloads ~815 MB on first use) catches scenes NudeNet misses: wide, dim or partly hidden frames. It only runs on frames NudeNet did *not* flag, and, unless *Ignore end credits* is turned off, never after the title's Plex credits marker (credit rolls read as nudity). It embeds the whole frame once and compares it with short text descriptions (`"bare buttocks"`, `"people having sex"`, but also counter-examples like `"a couple fully clothed in bed"`, `"end credits scrolling text"`, `"a landscape"`). The frame's score is the share of probability that lands on descriptions of labels you enabled, so clothed or ordinary scenes pull it down. A hit is tagged with the existing label whose own descriptions scored highest, is judged by that label's SigLIP threshold, and must repeat on *SigLIP Consecutive Frames* sampled frames before it becomes a segment. SigLIP segments are marked as such (source `siglip`) so you can tell them apart and threshold them separately.

**Segments** group flagged frames closer together than the *Segment Merge Gap* (`12 s`). A cluster needs *Minimum Hits* flagged frames to be kept. Severity follows the labels (see [Categories, severity and actions](segment-expansion-and-skip-logic.md#categories-severity-and-actions)).

**False-positive verifier** (`off` by default) is a small filter trained on *your* verdicts. Frames you marked as false positives are compared with frames of segments you kept, using SigLIP's image embedding, and a separate classifier per detector **and label** (for example SigLIP hits on `BUTTOCKS_EXPOSED`) learns what your rejects look like. A label that does not yet have at least 30 rejected and 30 kept examples has no model, so its detections are never rejected; rejections you made under one label cannot affect another. At scan time a flagged segment whose peak frame looks like them is **rejected**: stored inactive, with its frame, under **Analytics → False positives** (filter *Rejected by the verifier*), where the undo button restores a wrong rejection.

Setup:

1. Mark false positives as you review
2. Go to **Settings → SigLIP Fallback → Train** (needs at least 30 false positives and 30 kept segments for a label, and SigLIP's models)
3. Pick a level, save, and rescan (*Rescan Titles → scanned by an older scanner*)

The level is how many good segments you accept losing: **5% Gentle, 10% Balanced, 20% Strict**. Retrain after you have marked more.

## Tuning accuracy

1. Review segments and mark wrong ones as false positives (the thumbs-down on a segment); correct wrong labels with the tag button. Both are kept as feedback.
2. Open **Analytics → Accuracy** to see, per detector and label, how often detections were marked false and the mean scores of kept versus marked ones.
3. Raise the threshold of labels with a high false-positive rate (**Settings → NudeNet Detection / SigLIP Fallback → per-label thresholds**). Raising a threshold also drops some real detections, so move it a little at a time. SigLIP is weakest on the genital labels, where it reacts to the scene rather than the body part; give those a high threshold or leave the label out of *Detector Labels*, which stops both detectors flagging it.
4. Train the verifier (above) to catch what thresholds cannot, such as clothed bed scenes, swimming, credits and skin close-ups.
5. Rescan to apply new settings to titles already scanned: **Settings → Rescan Titles** with *Scanned by an older scanner* after an update, or *Titles that have segments*.

Detection is statistical: expect some misses and some false positives, and review what matters to you. [Imported skip files](../guide/importing-skip-files.md) are human-graded and are the most accurate source where they exist.
