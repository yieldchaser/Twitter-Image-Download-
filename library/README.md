# library/

Curated, browsable view of the X/Twitter image archive collected by the
downloader. Images are grouped by content type, then by recurring series,
so weekly reports, daily tables, and repeating chart packs sit together in
chronological order.

Type folders (8):

- `charts/` (1791 files) - market and price charts: freight, tanker, commodity, equity.
- `commentary/` (336 files) - text commentary screenshots and discussion clippings.
- `documents/` (1369 files) - research notes, earnings results, press releases, broker reports.
- `maps/` (170 files) - vessel-tracking and geography/weather maps.
- `news/` (453 files) - news article clippings grouped by outlet.
- `other/` (515 files) - memes, UI screenshots, and miscellaneous images.
- `photos/` (559 files) - vessel, port, people, and event photographs.
- `tables/` (1296 files) - rate tables, quotes, fixtures, and market statistics.

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
