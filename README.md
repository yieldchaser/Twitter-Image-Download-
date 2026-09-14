# X / Twitter Image Archive

Automated archive of image media posted by selected public X accounts.

## Tracked accounts

- `@HBGrimes`
- `@casharmax`
- `@MarhelmData`
- `@ole_sanne`

## How it works

The GitHub Action uses the X API with the `X_BEARER_TOKEN` repository secret. It paginates through each account's available posts, finds attached media, downloads the original image URL when available, and commits new files to the repository.

Files are named from the post date, account, and a sanitized version of the post text, for example:

`2026-08-23_casharmax_Tanker_secondhand_values_Xclusiv_abc123.jpg`

A JSONL metadata file is also maintained for each account under `metadata/`, containing the post ID, complete post text, X URL, media key, source media URL, filename, and alt text when supplied by X.

## Credential

Create a repository Actions secret named `X_BEARER_TOKEN`. Never commit the token to the repository.

## Runs

- Manual: GitHub Actions → **Download X Images** → **Run workflow**
- Scheduled: daily
- Also runs after changes are pushed to `main`

## Important API limitation

The number of historical posts/media that can be retrieved depends on the X API access level associated with the token. The downloader will retrieve everything the API makes available and paginate through it; it cannot bypass X API limits.

## Curated library

Browsable, curated copy of the archive lives under `library/` (6495 images
in 666 series across 8 type folders), grouped by content type and recurring
series. See `library/README.md` for the folder guide, `library/INDEX.md`
for the per-series listing, and `library/catalog.csv` for the per-image
metadata table.

Pipeline: gallery-dl (pinned to 1.32.11; 1.32.12 breaks X extraction with
a Cloudflare 403) downloads each account in a per-account matrix job, then
a single downstream `file` job runs `downloader/classify_new.py`. It
auto-files exact reposts (sha1 already in the catalog) and template matches
(dHash distance <= 6 with a margin of >= 4 over the runner-up series;
near-identical layouts at distance <= 2 file with no margin required),
removes redundant re-downloads already archived on disk, and queues the
rest in `library/_inbox/<account>/` for review.
