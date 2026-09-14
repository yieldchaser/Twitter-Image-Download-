# library/

Curated, browsable view of the X/Twitter image archive collected by the
downloader. Images are grouped by content type, then by recurring series,
so weekly reports, daily tables, and repeating chart packs sit together in
chronological order.

Type folders (8):

- `charts/` (1792 files) - market and price charts: freight, tanker, commodity, equity.
- `commentary/` (336 files) - text commentary screenshots and discussion clippings.
- `documents/` (1370 files) - research notes, earnings results, press releases, broker reports.
- `maps/` (170 files) - vessel-tracking and geography/weather maps.
- `news/` (453 files) - news article clippings grouped by outlet.
- `other/` (517 files) - memes, UI screenshots, and miscellaneous images.
- `photos/` (560 files) - vessel, port, people, and event photographs.
- `tables/` (1297 files) - rate tables, quotes, fixtures, and market statistics.

Series folders hold one chronological recurring publication each: weekly
broker reports (e.g. Gibson, Affinity), daily rate stipulation tables
(e.g. Infinity), earnings-result packs, and repeating chart series. Series
with a single image are kept as one-file folders so new posts extend them.

Filenames are preserved from the download (`date_account_tweetid_num`) so
files sort by date inside each folder and stay traceable to the source post.

Finding things:

- `INDEX.md` - per-type listing of every series with file counts, date ranges,
  accounts, and an example title.
- `catalog.csv` - one row per image; filter by account, date, tweet ID,
  publisher, subject, or series. The `target` column is the path under `library/`.

Note: `images/` remains the raw downloader sync area. New downloads land under
`images/<account>/`, and the per-tweet metadata JSONs stay under
`images/<account>/metadata/`.

New arrivals & review inbox
---------------------------

After each download, `downloader/classify_new.py` runs once in a single
downstream `file` job (downloads run first in a per-account matrix with
gallery-dl pinned to 1.32.11; 1.32.12 breaks X extraction with a Cloudflare
403) and files the new images into the library automatically:

- Redundant re-downloads already archived on disk are removed: when
  (account, tweet_id, num) already has a catalog row whose target exists,
  the new file is deleted after logging `already archived at <target>,
  removing redundant download`. The same log-and-delete applies to the
  byte-identical case (sha1 already in the catalog and its target exists).
  The (account, tweet_id, num) check runs before hashing, so it also
  catches re-encoded re-downloads that sha1 matching misses.
- Exact reposts (sha1 already in `catalog.csv`) are filed into the same
  series as the earlier copy.
- Otherwise the image's dHash (perceptual hash) is compared against
  `phashes.json`, which holds one hash per library image. A near-identical
  layout (best distance <= 2) inherits that series' type, publisher, and
  recurrence with no margin required. Otherwise the closest template
  series wins when it is close (at most `PHASH_AUTO_MAX = 6` bits
  different) and unambiguous (at least `PHASH_MARGIN_MIN = 4` bits ahead of
  the runner-up series). Auto-filed rows are logged in `catalog.csv` as
  `auto-filed ...`.
- Anything else lands in `library/_inbox/<account>/` with catalog
  `type='inbox'` and `notes='needs review'`, and shows up in `INDEX.md`
  under an `_inbox arrivals` section until the inbox is empty.

Review flow for inbox items (human or agent): inspect the image and its
tweet text (via the `subject` column or `images/<account>/metadata/`), move
the file into the right `library/<type>/<series>/` folder (creating the
series folder when it starts a new recurring publication), then update that
row in `catalog.csv` — set `file`/`target` to the new path and fill in the
real `type`, `series`, `title`, `publisher`, and `recurrence`. The next
classifier run picks up the corrected row, refreshes `phashes.json`, and
regenerates `INDEX.md`.
