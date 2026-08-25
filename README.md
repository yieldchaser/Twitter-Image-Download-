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
