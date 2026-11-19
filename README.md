# Colvia Preview downloads

| | English | 한국어 |
| --- | --- | --- |
| Install or update · 설치·업데이트 | [Copy the command for your platform](https://colvia.github.io/downloads/#en) | [내 운영체제에 맞는 명령 복사](https://colvia.github.io/downloads/#ko) |
| Preview releases · 배포판 | [Release notes, ZIP files and checksums](https://github.com/colvia/downloads/releases) | [릴리스 노트·ZIP·체크섬](https://github.com/colvia/downloads/releases) |
| Source and user guide · 소스·사용 안내 | [Colvia on GitHub](https://github.com/kyungseo/colvia) | [Colvia 한국어 README](https://github.com/kyungseo/colvia/blob/main/README.ko.md) |

To update, run the same installation command again in the account where you installed Colvia. 업데이트할 때는 Colvia를 설치한 계정에서 같은 설치 명령을 다시 실행하세요.

## Publishing a Preview

Run these files in `colvia/downloads`. The workflow consumes an already signed, approved snapshot. It publishes the Pages snapshot, checks the files at their public HTTPS URLs, then publishes a GitHub prerelease with the exact same three platform ZIPs and their SHA-256 checksums.

## Repository setup

Use GitHub Actions as the Pages source and install this workflow on the default `main` branch. Keep new product releases manually approved. Configure the `release-publication` environment with a required reviewer who can approve the official installation verification. GitHub allows required reviewers on public repositories. The initial publication always waits in this environment after the public URL checks. Enable `review_installation` on later versions that require a new installation or update check. Other updates proceed after the automated checks. [GitHub Pages workflow](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages) · [Deployment environments](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments)

GitHub's Release list returns drafts to callers with repository write access. The Pages job uses the same-repository token with `contents: write` to read those drafts. Its preparation step reads the draft and its assets. The public Pages check runs without a token. The Release job uses the repository token to attach files and publish the final draft. [Releases API](https://docs.github.com/en/rest/releases/releases#list-releases)

## Prepare and review the files

1. Assemble a full site tree containing `index.html`, the three `preview/<platform>/install.*` scripts, and each platform's `bootstrap/`, `metadata/` and `targets/` files. Use the `preview` channel and the fixed base URL `https://colvia.github.io/downloads/`. Use versioned TUF metadata and SHA-256-prefixed consistent-snapshot target names. Name bootstrap bundles `bootstrap/<sha256>.zip` and use the matching URL, byte length and hash in the installer. Keep previous versioned files in the tree. The workflow supplies `preview/publication.json`, the approved manifest, and the original timestamps.
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

## Refresh the signed timestamps

Enable **Refresh Colvia Preview timestamps** after verifying the approved manifest and the three original timestamps on Pages. The schedule runs on Tuesdays at 02:23 UTC. Use the timestamp job on `main` for a first manual check after setting the repository variable described below. Use the `preview-timestamp` environment restricted to `main` with the timestamp role's Ed25519 private key as `COLVIA_TIMESTAMP_KEY_B64` (standard base64 of the 32-byte private seed). Keep the other signing roles offline. The scheduled and manual triggers use the same job and share a deployment queue with the release workflow.

When a manual Pages publication has been verified, set the repository Actions variable `COLVIA_APPROVED_MANIFEST_SHA256` to the same digest used for its approval. The signed ZIPs and release approval remain fixed during timestamp refreshes. Each manual publication supplies the exact approved manifest and the three original timestamps alongside the deployed files. The refresh checks them against the approved digest, verifies the current signed metadata, and replaces only the three timestamps and `preview/timestamp-publication.json`. Existing signed files are retained byte for byte. A new release whose timestamp has been overtaken needs a newly approved manifest with an advancing timestamp version.

Click **Run workflow** on `main` to recover from an expired timestamp or a transient failed run. The job rereads the current publication. It can use a still-valid signed snapshot and targets even after the old timestamp expires. A failed pre-deployment check leaves Pages intact. If public verification failed after deployment, inspect the job summary and the live publication before starting another run.

In the existing release operations record, keep the date of the last successful weekly timestamp refresh and its Actions run link, and the next check date. Set the first check for after the first expected scheduled run once the workflow is enabled. At a check, use the last run that verified the published timestamps, retain that success date if a later run failed, and enter the next check date. Until the first successful scheduled run, leave the success date pending. About six weeks after the latest manual release publication or offline metadata renewal, if no new version is planned, check the schedule, the last success, and the snapshot and targets expiry dates together. The job writes the earliest upstream expiry in its summary and uses GitHub's failed-run notification when action is needed within 21 days. The summary says whether the timestamp refresh itself succeeded. A disabled schedule needs this manual check.

When preparing release signing and at the six-week check during a release gap, read the expiry from the current trusted root metadata and check the offline storage of the root keys and recovery copies using the existing custody procedure. Record the expiry and the next check in the private signing record. If renewal is needed, schedule preparation before expiry under the separate root-rotation procedure.

### Add the baseline files to the initial v0.1.1 publication

With publication approval, reuse the existing `staging-v0.1.1` draft and its approved manifest SHA-256. Run **Publish approved Colvia Preview** on `main` with `tag: v0.1.1`, the approved `manifest_sha256`, `pages_only: true`, and `seed_timestamp_baseline: true`.

The workflow verifies the live publication marker and every approved file. If all four baseline files are absent and no refresh record exists, it adds `preview/publish-manifest.json` and `preview/<platform>/approved-timestamp.json` for each of the three platforms. It preserves all existing Pages bytes. If the four files already match, the workflow skips deployment. A mismatch or a partial set stops the run. The Release job is skipped in this mode.

After the public verification passes, set `COLVIA_APPROVED_MANIFEST_SHA256` to that digest and run **Refresh Colvia Preview timestamps** manually once. Confirm that all three timestamps advance and note the result. Record the first successful scheduled run separately.

### Keep a release available past 90 days

Before targets or snapshot expires, sign new 90-day metadata offline over the same approved ZIPs. Advance targets, snapshot, and timestamp metadata versions and retain the earlier versioned files. Obtain manual approval for one Pages-only maintenance publication with a separate publication identity. Keep the product version, ZIPs, install commands, tags and Release entries. The maintenance publication path needs its own implementation and verification before that operation. The weekly job can renew a timestamp while its referenced upstream metadata remains valid.
