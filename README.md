# downloads

Tooling for https://downloads.arkelectron.com, an S3 bucket behind CloudFront.

## Layout

| Path | Contents | Cached |
|---|---|---|
| `<folder>/<product>/<version>/` | A release's files and its `SHA256SUMS`. Written once, never replaced. | 1 year |
| `<folder>/<product>/latest/` | The promoted release under fixed names, plus `VERSION` | 60 s |
| `manifest.json` | Every file listed in a `SHA256SUMS`, with its sha256 and size | 60 s |
| `.../index.html` | Generated folder pages from `page.html`; `aws/dir-index.js` serves them for `<dir>/` | 60 s |

Folders: `firmware/` (private_px4), `jetson/` (ark_jetson_kernel), `pi/` (ark_pi_image), `dev/` (dev builds, below).

## Publishing from a workflow

```yaml
jobs:
  downloads:
    runs-on: ubuntu-latest
    environment: downloads
    permissions:
      contents: read
      id-token: write
    steps:
      - uses: actions/checkout@v4
        with:
          repository: ARK-Electronics/downloads
          path: downloads
      - uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ vars.AWS_ROLE_ARN }}
          aws-region: us-west-1
          mask-aws-account-id: true
      - env:
          DOWNLOADS_BUCKET: ${{ vars.DOWNLOADS_BUCKET }}
        run: |
          downloads/downloads.py publish <dir> <folder>/<product>/<version>
          downloads/downloads.py promote <folder>/<product>/<version> <folder>/<product>/latest
          downloads/downloads.py index
```

`publish` checks every file against the directory's `SHA256SUMS` and refuses a version that already exists. `--exclude '*.elf'` keeps files off the site.

## Adding a repo

1. In the repo's settings, create environment `downloads` and limit it to the release tags and `main`.
2. With admin credentials: `aws/create_publish_role.sh <owner/repo> <folder> <bucket>`. The role trusts only that environment and writes only `<folder>/`, the index pages and the manifest.
3. Give the environment variables `AWS_ROLE_ARN` (printed by step 2) and `DOWNLOADS_BUCKET`.

Locally, any credentials that can write the bucket run the same commands with `DOWNLOADS_BUCKET` set, e.g. to backfill releases.

## Dev builds

`dev/<folder>/<product>/<branch>-<sha8>/` holds builds shared by direct link, for our own testing or for a customer to try. The index pages and manifest skip `dev/`, and a lifecycle rule deletes it 90 days after upload. There are no folder pages under `dev/`, so share the file's URL.

Publish one from a local build with any credentials that can write the bucket:

```bash
(cd <build dir> && sha256sum -- * > SHA256SUMS)
DOWNLOADS_BUCKET=<bucket> AWS_REGION=us-west-1 ./downloads.py publish <build dir> dev/firmware/ark_g5-heading-gps/main-1a2b3c4d
```

The file is then at `https://downloads.arkelectron.com/dev/firmware/ark_g5-heading-gps/main-1a2b3c4d/<file>`.
