# RND-207 Performance Baseline Method

A repeatable way to measure image-loading performance **before** and **after**
the RND-207 changes (thumbnails, fixed-window signed URLs, lazy/async decode,
CLS reservation). Run the identical procedure pre- and post-change on the same
conversation and browser profile so the numbers are comparable.

> **Do not fabricate numbers.** Any metric that can only be measured against
> real production traffic (real CDN-vs-origin egress, real object sizes) is
> marked **PENDING PRODUCTION** below and must be filled in from an actual
> production measurement, not estimated.

## What to measure

For a chosen conversation with a representative number of image messages:

| Metric | How |
|---|---|
| First-screen load time | DevTools Performance/Network, time to timeline painted |
| Image request count | Network tab, filter `Img` |
| Image bytes transferred | Network tab, "Transferred" summed over image requests |
| Duplicate request count | Same URL requested more than once (see HAR script below) |
| Duplicate request ratio | duplicates ÷ total image requests |
| Original-image request count | requests WITHOUT `variant=thumb` |
| Thumbnail request count | requests WITH `variant=thumb` |
| Second-visit bytes | reload the page (F5) and re-sum transferred image bytes |
| New image requests after one auto-refresh | wait one 30s refresh cycle, count NEW image requests |

## Procedure

1. Open the conversation in a clean browser profile (empty cache). Open
   DevTools → Network, disable cache **off** (we WANT to observe caching),
   filter to `Img`.
2. Record first-screen load time and the image request count / bytes.
3. Export the HAR (Network → right-click → "Save all as HAR").
4. Press **F5** (hard reload is NOT wanted — we want normal reload to observe
   browser HTTP cache reuse). Record second-visit image bytes.
5. Leave the tab focused for one 30s auto-refresh cycle. Record how many NEW
   image requests appear (expected: ~0 for unchanged messages — RND-204 +
   RND-207 stable-URL behavior).
6. Open one image in the viewer. Confirm it requests the ORIGINAL (no
   `variant=thumb`) and that list images did not.

## HAR analysis helper

Count total/duplicate/thumbnail/original image requests from an exported HAR:

```python
import json, sys
from collections import Counter

har = json.load(open(sys.argv[1]))
entries = [e for e in har["log"]["entries"]
           if e["response"]["content"].get("mimeType", "").startswith("image/")]
urls = [e["request"]["url"] for e in entries]
counts = Counter(urls)
total = len(urls)
dupes = sum(c - 1 for c in counts.values() if c > 1)
thumbs = sum(1 for u in urls if "variant=thumb" in u)
bytes_ = sum(e["response"].get("_transferSize", e["response"]["bodySize"] or 0)
             for e in entries)
print(f"image requests:        {total}")
print(f"unique image urls:     {len(counts)}")
print(f"duplicate requests:    {dupes} ({(dupes/total*100 if total else 0):.1f}%)")
print(f"thumbnail requests:    {thumbs}")
print(f"original requests:     {total - thumbs}")
print(f"image bytes transferred: {bytes_}")
```

Run pre- and post-change:
```bash
python analyze_har.py before.har
python analyze_har.py after.har
```

## Expected direction of change (to be confirmed with real measurements)

- **Image bytes transferred (list):** ↓ substantially — list now loads
  ~360px thumbnails instead of full originals.
- **Duplicate requests / second-visit bytes:** ↓ — fixed-window signed URLs are
  byte-identical within the window, so the browser reuses cached bytes on
  reload instead of re-downloading a freshly-signed URL.
- **New image requests after auto-refresh:** ~0 for unchanged messages
  (RND-204 incremental refresh + stable URLs).
- **Original-image requests:** only when the viewer is opened.

## PENDING PRODUCTION (cannot be measured locally)

- Real CDN-vs-origin egress and request-count deltas (needs production traffic
  + Qiniu billing console).
- Real average object-size reduction across the historical image corpus (needs
  the production media set / completed backfill).
- Whether the origin domain returns `Cache-Control`/`ETag` enabling 304
  revalidation (needs the bound origin domain — see the migration runbook's
  external-verification items).
