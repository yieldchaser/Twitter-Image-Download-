# X / Twitter Image Archive

Automated archive of image media posted by selected public X accounts.

## Tracked accounts

- `@HBGrimes`
- `@casharmax`
- `@MarhelmData`
- `@ole_sanne`
- `@ed_fin`

## How it works

The GitHub Action (**Download X Images**) runs gallery-dl with the `X_COOKIES` repository secret. A per-account matrix job pages each account's media timeline (plus deep-backfill search windows on demand), downloads new images, and commits them; a single downstream job then files the newcomers into `library/`.

Files keep downloader names built from post date, account, tweet ID, and image number, for example:

`2026-08-23_casharmax_1831044222960681371_1.jpg`

Per-tweet metadata JSONs sit alongside under `images/<account>/metadata/`, and per-account run status under `metadata/status/`.

## Credential

Create a repository Actions secret named `X_COOKIES` holding a Cookie-Editor JSON export of x.com cookies. Never commit cookies to the repository. Refresh the export when the session expires.

## Runs

- Manual: GitHub Actions → **Download X Images** → **Run workflow** (all accounts or one)
- Scheduled: daily 20:17 UTC
- Also runs after human pushes to `main` (the bot's own commits don't retrigger it)

## Limitations

X rate-limits and Cloudflare-protects automated access: gallery-dl is pinned to a known-good version (see below) and runs fail loudly at validation rather than silently when X changes its defenses. Historical reach depends on what X serves the logged-in session.

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
