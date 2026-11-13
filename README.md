# Colvia Preview publication

Run these files in `colvia/downloads`. The workflow consumes an already signed, approved snapshot. It publishes the Pages snapshot, checks the files at their public HTTPS URLs, then publishes a GitHub prerelease with the exact same three platform ZIPs and their SHA-256 checksums.

## Repository setup

Use GitHub Actions as the Pages source and install this workflow on the default `main` branch. Keep publication manual. Configure the `release-publication` environment with a required reviewer who can approve the official installation verification. GitHub allows required reviewers on public repositories. The initial publication always waits in this environment after the public URL checks. Enable `review_installation` on later versions that require a new installation or update check. Other updates proceed after the automated checks. [GitHub Pages workflow](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages) · [Deployment environments](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments)

GitHub's Release list returns drafts to callers with repository write access. The Pages job uses the same-repository token with `contents: write` to read those drafts. Its preparation step reads the draft and its assets. The public Pages check runs without a token. The Release job uses the repository token to attach files and publish the final draft. [Releases API](https://docs.github.com/en/rest/releases/releases#list-releases)

## Prepare and review the files

1. Assemble a full site tree containing `index.html`, the three `preview/<platform>/install.*` scripts, and each platform's `bootstrap/`, `metadata/` and `targets/` files. Use the `preview` channel and the fixed base URL `https://colvia.github.io/downloads/`. Use versioned TUF metadata and SHA-256-prefixed consistent-snapshot target names. Name bootstrap bundles `bootstrap/<sha256>.zip` and use the matching URL, byte length and hash in the installer. Keep previous versioned files in the tree. The workflow supplies `preview/publication.json`.
2. Put the approved release notes at the tree root as `RELEASE-NOTES.md`. Create `spec.json` with the following shape. Replace the placeholders with actual reviewed values. Use JSON `null` for `previous_manifest_sha256` only for the first publication. On updates use the manifest SHA-256 shown in the live publication marker.

```json
{
  "tag": "v0.1.0-preview.1",
  "source_commit": "<40-hex Colvia source commit>",
  "previous_manifest_sha256": null,
  "root_sha256": "<64-hex SHA-256 of the embedded trust root>",
  "platforms": {
    "macos-arm64": {
      "bootstrap": "preview/macos-arm64/bootstrap/<bootstrap-sha256>.zip",
      "target": "colvia-macos-arm64.zip"
    },
    "windows-x64": {
      "bootstrap": "preview/windows-x64/bootstrap/<bootstrap-sha256>.zip",
      "target": "colvia-windows-x64.zip"
    },
    "windows-arm64": {
      "bootstrap": "preview/windows-arm64/bootstrap/<bootstrap-sha256>.zip",
      "target": "colvia-windows-arm64.zip"
    }
  },
  "release_zips": {
    "colvia-v0.1.0-preview.1-macos-arm64.zip": "preview/macos-arm64/targets/<target-sha256>.colvia-macos-arm64.zip",
    "colvia-v0.1.0-preview.1-windows-x64.zip": "preview/windows-x64/targets/<target-sha256>.colvia-windows-x64.zip",
    "colvia-v0.1.0-preview.1-windows-arm64.zip": "preview/windows-arm64/targets/<target-sha256>.colvia-windows-arm64.zip"
  }
}
```

3. Run `pack` with the pinned verifier libraries from `scripts/requirements-publish.txt` or the already qualified verifier supply on a compatible Python. Use new directories for the output and check workspace. This command generates the two staging assets, verifies their bytes, and reads the signed TUF chain using the trust root embedded in the approved bootstrap. It does not use a GitHub login.

```sh
python3 -B scripts/publish.py pack \
  --spec /path/to/spec.json --source /path/to/site-input \
  --output /path/to/new-staging --work /path/to/new-pack-check
```

4. Review the manifest, version, commit, root hash, release notes and resulting SHA-256. Keep the approved files and digest. Use the following to repeat the local validation and assemble the Pages tree without GitHub access.

```sh
RELEASE_TAG=v0.1.0-preview.1 MANIFEST_SHA256='APPROVED_MANIFEST_SHA256' \
  python3 -B scripts/publish.py local --manifest /path/to/publish-manifest.json \
  --archive /path/to/publish-input.zip --work /path/to/new-check
```

Replace `APPROVED_MANIFEST_SHA256` before copying the command. All tree files are public release inputs. Keep keys, tokens, internal logs and unpublished source out of the tree and release notes.

## Publish or resume

1. After publication approval, upload only `publish-manifest.json` and `publish-input.zip` to the draft named by the tag `staging-<tag>` in `colvia/downloads`. Save it as a draft. Start **Publish approved Colvia Preview** on `main` with `tag`, the approved `manifest_sha256`, and `review_installation` when applicable.
2. Pages is published first. The verification job fetches every approved Pages file anonymously, requires a direct HTTPS 200, and compares sizes and SHA-256. If the release job waits for review, check the official install and same-command update under the approved platform plan, then approve that waiting job. The Release job rechecks the Pages bytes and TUF trust before attaching and publishing files. It then downloads the publicly served attachments and verifies the bytes. Keep the staging draft and the exact local files for interrupted runs.
3. To qualify the first public upgrade with two separately approved snapshots, run the baseline with `pages_only: true`. That run stops after verifying the Pages files. Install the baseline from the official URL on the designated test account, then run the successor with `review_installation: true`, using the baseline manifest digest as its predecessor. Check the update before approving the waiting Release job. The regular workflow defaults to publishing the Release after the applicable checks.
4. For a transient failure, use **Re-run failed jobs** on the same Actions run. When rerunning the whole workflow, it skips Pages deployment if the live marker and files match. For an existing Release draft, it keeps identical attachments and uploads missing ones. For an already public Release it verifies the existing attachments. Resume using the same inputs and workflow commit. If that is no longer possible, reconcile the live publication and draft before starting another run. [Re-running GitHub Actions jobs](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/re-run-workflows-and-jobs)

Stop and inspect if the live predecessor is different, signatures have expired, a hash differs, an attachment is unexpected, or a final tag already points at a different commit. Preserve the original files and run record. An updated manifest needs its own approval. To correct a live release, keep update version counters monotonic and follow the approved recovery procedure. The workflow supports releases under the same pinned root and carries forward prior immutable files. Root rotation or pruning retained files needs a separate validated procedure.

The official URL and live GitHub run are verified at release time. Local checks cannot establish GitHub runner dependencies, remote permissions, production installation or update behavior.
