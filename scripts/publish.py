"""Publish one pre-approved Colvia Preview bundle. Local validation never uses GitHub."""
import argparse
from datetime import datetime, timezone
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile

REPO = "colvia/downloads"
BASE = "https://colvia.github.io/downloads/"
MARKER = "preview/publication.json"
APPROVAL = "preview/publish-manifest.json"
FRESHNESS = "preview/timestamp-publication.json"
MANIFEST = "publish-manifest.json"
ARCHIVE = "publish-input.zip"
PLATFORMS = {"macos-arm64": "install.sh", "windows-x64": "install.ps1",
             "windows-arm64": "install.ps1"}
TAG = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9]+(?:[.-][a-zA-Z0-9]+)*)?")
SHA = re.compile(r"[0-9a-f]{64}")
SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}")
CHANNEL = "preview"
ROLE = re.compile(r"(?:[1-9][0-9]*\.(?:root|snapshot|targets)|timestamp)\.json")
LIMIT = 950_000_000  # Keep the published snapshot below the Pages 1 GB limit.
JSON_LIMIT = 256 * 1024
CHUNK = 1024 * 1024


class Stop(Exception):
    pass


def require(condition, message):
    if not condition:
        raise Stop(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def encode(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def bounded_bytes(path, limit=JSON_LIMIT):
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    require(len(data) <= limit, f"file too large: {path.name}")
    return data


def load_json(raw):
    def distinct(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, f"duplicate JSON key: {key}")
            result[key] = value
        return result
    return json.loads(raw.decode("utf-8"), object_pairs_hook=distinct)


def is_sha(value):
    return isinstance(value, str) and SHA.fullmatch(value) is not None


def is_tag(value):
    return isinstance(value, str) and TAG.fullmatch(value) is not None and len(value) <= 80


def safe_path(path):
    return isinstance(path, str) and all(SEGMENT.fullmatch(part) and not part.endswith(".")
                                        for part in path.split("/"))


def pin(stream, limit, output=None):
    size = 0
    digest = hashlib.sha256()
    while chunk := stream.read(CHUNK):
        size += len(chunk)
        require(size <= limit, "file exceeds its approved size")
        digest.update(chunk)
        if output is not None:
            output.write(chunk)
    return {"bytes": size, "sha256": digest.hexdigest()}


def file_pin(path, limit=LIMIT):
    with path.open("rb") as stream:
        return pin(stream, limit)


def valid_pin(row):
    return (isinstance(row, dict) and set(row) == {"bytes", "sha256"} and
            type(row["bytes"]) is int and 0 < row["bytes"] <= LIMIT and is_sha(row["sha256"]))


def site_files(value):
    require(isinstance(value, dict) and 0 < len(value) <= 4096, "invalid Pages file list")
    require(len({p.casefold() for p in value}) == len(value), "case collision in Pages files")
    for path, row in value.items():
        require(safe_path(path) and path != MARKER and valid_pin(row), f"invalid Pages file: {path}")
        if path == "index.html":
            continue
        parts = path.split("/")
        require(len(parts) in (3, 4) and parts[0] == "preview" and parts[1] in PLATFORMS,
                f"unexpected Pages path: {path}")
        if len(parts) == 3:
            require(parts[2] == PLATFORMS[parts[1]], f"unexpected installer: {path}")
            continue
        folder, name = parts[2:]
        require(
            (folder == "bootstrap" and name == row["sha256"] + ".zip") or
            (folder == "metadata" and ROLE.fullmatch(name)) or
            (folder == "targets" and name.startswith(row["sha256"] + ".") and name.endswith(".zip")),
            f"unexpected or unpinned Pages file: {path}",
        )
    require(sum(row["bytes"] for row in value.values()) <= LIMIT - JSON_LIMIT, "Pages size limit")


@dataclass(frozen=True)
class Plan:
    tag: str
    digest: str
    data: dict
    raw: bytes = b""

    @property
    def marker(self):
        return encode({"tag": self.tag, "manifest_sha256": self.digest,
                       "root_sha256": self.data["root_sha256"], "pages": self.data["pages"]})

    def platform_zip(self, platform):
        cfg = self.data["platforms"][platform]
        if "retained_zip" in cfg:
            return cfg["retained_zip"]
        return self.data["release_zips"][f"colvia-{self.tag}-{platform}.zip"]

    @property
    def release_pins(self):
        pages = self.data["pages"]
        return {**{name: pages[path] for name, path in self.data["release_zips"].items()},
                "SHA256SUMS.txt": self.data["checksums"]}

    @property
    def archive_pins(self):
        return {**self.data["pages"], "RELEASE-NOTES.md": self.data["notes"],
                "SHA256SUMS.txt": self.data["checksums"]}


def read_plan(raw, tag, digest):
    require(is_tag(tag) and is_sha(digest), "invalid release tag or approval digest")
    require(len(raw) <= JSON_LIMIT and sha(raw) == digest, "manifest differs from approval")
    data = load_json(raw)
    require(isinstance(data, dict) and set(data) == {
        "schema", "tag", "channel", "source_commit", "base_url", "previous_manifest_sha256",
        "root_sha256", "pages", "platforms", "release_zips", "checksums", "notes",
    }, "invalid manifest fields")
    require(type(data["schema"]) is int and data["schema"] in (1, 2) and data["tag"] == tag and
            data["channel"] == CHANNEL and data["base_url"] == BASE and
            isinstance(data["source_commit"], str) and
            re.fullmatch(r"[0-9a-f]{40}", data["source_commit"]), "invalid release identity")
    previous = data["previous_manifest_sha256"]
    require(previous is None or (is_sha(previous) and previous != digest), "invalid prior manifest")
    require(is_sha(data["root_sha256"]), "invalid trust root pin")
    pages = data["pages"]
    site_files(pages)
    require("index.html" in pages and pages["index.html"]["bytes"] <= JSON_LIMIT, "missing Pages entry")
    require(isinstance(data["platforms"], dict) and set(data["platforms"]) == set(PLATFORMS),
            "expected all three platform configurations")
    expected_zips = {}
    zips = data["release_zips"]
    require(isinstance(zips, dict) and zips, "expected at least one Release ZIP")
    for platform, entry_name in PLATFORMS.items():
        prefix = f"preview/{platform}/"
        cfg = data["platforms"][platform]
        fields = {"bootstrap", "target"}
        require(isinstance(cfg, dict) and (set(cfg) == fields or
                (data["schema"] == 2 and set(cfg) == fields | {"retained_zip"})),
                f"invalid {platform} configuration")
        bootstrap, target = cfg["bootstrap"], cfg["target"]
        require(isinstance(bootstrap, str) and bootstrap.startswith(prefix + "bootstrap/") and
                bootstrap in pages and safe_path(target) and "/" not in target and target.endswith(".zip"),
                f"invalid {platform} bootstrap or target")
        script = prefix + entry_name
        timestamp = prefix + "metadata/timestamp.json"
        require(script in pages and pages[script]["bytes"] <= JSON_LIMIT and
                timestamp in pages and pages[timestamp]["bytes"] <= JSON_LIMIT,
                f"missing {platform} script or timestamp")
        found = [path for path in pages if path.startswith(prefix + "targets/") and
                 path == prefix + "targets/" + pages[path]["sha256"] + "." + target]
        # The selected target is explicit in the release map when older target bytes are retained.
        asset_name = f"colvia-{tag}-{platform}.zip"
        if "retained_zip" in cfg:
            require(previous is not None and cfg["retained_zip"] in found,
                    f"invalid {platform} retained ZIP or missing predecessor")
            continue
        require(zips.get(asset_name) in found, f"invalid {platform} release ZIP")
        expected_zips[asset_name] = zips[asset_name]
    require(data["release_zips"] == expected_zips, "unexpected Release attachments")
    require(len(Plan(tag, digest, data).marker) <= JSON_LIMIT, "publication marker is too large")
    for name in ("notes", "checksums"):
        require(valid_pin(data[name]) and data[name]["bytes"] <= JSON_LIMIT, f"invalid {name}")
    return Plan(tag, digest, data, raw)


def unpack(plan, archive, work):
    require(archive.stat().st_size <= LIMIT, "staged archive exceeds size limit")
    payload = work / "payload"
    payload.mkdir()
    expected = plan.archive_pins
    with zipfile.ZipFile(archive) as z:
        members = z.infolist()
        require(len(members) == len(expected) and {m.filename for m in members} == set(expected),
                "staged archive file list differs from manifest")
        for member in members:
            name = member.filename
            mode = stat.S_IFMT(member.external_attr >> 16)
            require(not member.is_dir() and member.orig_filename == name and
                    mode in (0, stat.S_IFREG) and not member.flag_bits & 1 and
                    member.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED),
                    f"invalid archive member: {name}")
            expected_pin = expected[name]
            require(member.file_size == expected_pin["bytes"], f"archive size differs: {name}")
            dest = payload / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            with z.open(member) as source, dest.open("xb") as output:
                require(pin(source, expected_pin["bytes"], output) == expected_pin, f"archive hash differs: {name}")
    sums = "".join(f"{row['sha256']}  {name}\n" for name, row in sorted(plan.release_pins.items())
                   if name.endswith(".zip")).encode()
    require(bounded_bytes(payload / "SHA256SUMS.txt") == sums, "SHA256SUMS.txt differs from signed target files")
    notes = bounded_bytes(payload / "RELEASE-NOTES.md").decode("utf-8")
    require(notes.strip() and "\x00" not in notes, "invalid release notes")
    return payload


def make_input(spec_path, source, output, work):
    spec = load_json(bounded_bytes(spec_path))
    require(isinstance(spec, dict) and set(spec) == {
        "tag", "source_commit", "previous_manifest_sha256", "root_sha256", "platforms", "release_zips",
    }, "invalid local input specification")
    require(source.is_dir() and not source.is_symlink() and not output.exists(), "expected input tree and new output")
    paths = list(source.rglob("*"))
    require(all(not p.is_symlink() and (p.is_dir() or p.is_file()) for p in paths),
            "input tree contains a symlink or special file")
    files = {p.relative_to(source).as_posix(): p for p in paths if p.is_file()}
    require("RELEASE-NOTES.md" in files and "SHA256SUMS.txt" not in files, "expected release notes and generated checksums")
    notes = files.pop("RELEASE-NOTES.md")
    pages = {name: file_pin(path) for name, path in files.items()}
    require(isinstance(spec["release_zips"], dict), "invalid release ZIP mapping")
    sums = "".join(f"{pages[path]['sha256']}  {name}\n" for name, path in sorted(spec["release_zips"].items())).encode()
    schema = 2 if any("retained_zip" in cfg for cfg in spec["platforms"].values()) else 1
    data = dict(spec, schema=schema, channel=CHANNEL, base_url=BASE, pages=pages,
                notes=file_pin(notes, JSON_LIMIT), checksums={"bytes": len(sums), "sha256": sha(sums)})
    raw = encode(data)
    plan = read_plan(raw, spec["tag"], sha(raw))
    output.mkdir()
    archive = output / ARCHIVE
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_STORED) as z:
        for name, path in sorted(files.items()):
            z.write(path, name)
        z.write(notes, "RELEASE-NOTES.md")
        z.writestr("SHA256SUMS.txt", sums)
    work.mkdir()
    payload = unpack(plan, archive, work)
    verify_trust(plan, payload, work)
    (output / MANIFEST).write_bytes(raw)
    print(f"Prepared {plan.tag} for review, manifest SHA-256 {plan.digest}")


def zip_json(path, name, limit=JSON_LIMIT):
    with zipfile.ZipFile(path) as z:
        require(z.namelist().count(name) == 1 and z.getinfo(name).file_size <= limit,
                f"missing or oversized ZIP metadata: {name}")
        raw = z.read(name)
    return load_json(raw), raw


def platform_url(platform):
    return BASE + f"preview/{platform}/"


def verify_trust(plan, payload, work, *, channel=CHANNEL, timestamps=None):
    from tuf.api.exceptions import DownloadError, DownloadHTTPError, RepositoryError
    from tuf.ngclient import Updater
    from tuf.ngclient.fetcher import FetcherInterface

    class LocalFetcher(FetcherInterface):
        def _fetch(self, url):
            origin = platform_url(platform)
            require(url.startswith(origin), "TUF fetch outside the approved platform origin")
            path = prefix + url[len(origin):]
            if path not in plan.data["pages"]:
                raise DownloadHTTPError("absent from approved Pages snapshot", 404)
            if timestamps and path in timestamps:
                yield timestamps[path]
            else:
                with (payload / path).open("rb") as stream:
                    yield from iter(lambda: stream.read(CHUNK), b"")

    for platform, cfg in plan.data["platforms"].items():
        prefix = f"preview/{platform}/"
        bootstrap = cfg["bootstrap"]
        script = bounded_bytes(payload / (prefix + PLATFORMS[platform])).decode("utf-8")
        bootstrap_pin = plan.data["pages"][bootstrap]
        require(platform_url(platform) + bootstrap[len(prefix):] in script and bootstrap_pin["sha256"] in script and
                str(bootstrap_pin["bytes"]) in script and "\x00" not in script,
                f"{platform} installer does not pin the approved bootstrap")
        bundle = payload / bootstrap
        installation, _ = zip_json(bundle, "installation.json")
        _, anchor = zip_json(bundle, "trust/root.json")
        target_path = plan.platform_zip(platform)
        target_config, _ = zip_json(payload / target_path, "installation.json")
        _, target_root = zip_json(payload / target_path, "trust/root.json")
        target_manifest, target_manifest_raw = zip_json(payload / target_path, "manifest.json", 4 * 1024 * 1024)
        require(sha(anchor) == plan.data["root_sha256"] and target_root == anchor, f"{platform} embedded root differs")
        metadata_url = platform_url(platform) + "metadata/"
        target_url = platform_url(platform) + "targets/"
        expected = {"channel": channel, "metadata_url": metadata_url,
                    "target_url": target_url, "target": cfg["target"]}
        for installed in (installation, target_config):
            require(isinstance(installed, dict) and all(installed.get(key) == value for key, value in expected.items()),
                    f"{platform} bundle configuration differs")
        os_name, architecture = platform.split("-", 1)
        require(target_manifest.get("os") == os_name and target_manifest.get("architecture") == architecture
                and type(target_manifest.get("sequence")) is int and target_manifest["sequence"] > 0
                and isinstance(target_manifest.get("release"), str) and target_manifest["release"],
                f"{platform} target identity differs")
        if plan.data["schema"] == 2 and "retained_zip" not in cfg:
            require(target_manifest["release"] == plan.tag[1:], f"{platform} release version differs from tag")
        expected_custom = {"colvia": {
            "version": 1, "channel": channel, "os": os_name, "architecture": architecture,
            "release": target_manifest["release"], "sequence": target_manifest["sequence"],
            "manifest_sha256": sha(target_manifest_raw),
        }}
        metadata_dir = work / ("tuf-" + platform)
        metadata_dir.mkdir()
        updater = Updater(metadata_dir=str(metadata_dir), metadata_base_url=metadata_url,
                          target_base_url=target_url, fetcher=LocalFetcher(), bootstrap=anchor)
        try:
            updater.refresh()
            info = updater.get_targetinfo(cfg["target"])
        except (DownloadError, RepositoryError) as error:
            raise Stop(f"{platform} TUF verification failed: {error}") from error
        # Require a top-level target so the layout matches the installed Colvia verifier.
        trusted_targets = load_json(bounded_bytes(metadata_dir / "targets.json"))["signed"]["targets"]
        expected_pin = plan.data["pages"][target_path]
        require(info is not None and cfg["target"] in trusted_targets and
                info.length == expected_pin["bytes"] and info.hashes == {"sha256": expected_pin["sha256"]} and
                info.unrecognized_fields.get("custom") == expected_custom,
                f"{platform} signed target does not match the approved ZIP")
        require((payload / target_path).name == info.hashes["sha256"] + "." + cfg["target"],
                f"{platform} signed target path differs")
        # Use the actual TUF client's download path, which also checks consistent snapshots.
        verified = metadata_dir / "verified.zip"
        try:
            updater.download_target(info, filepath=str(verified))
        except (DownloadError, RepositoryError) as error:
            raise Stop(f"{platform} TUF target verification failed: {error}") from error
        require(file_pin(verified, expected_pin["bytes"]) == expected_pin, f"{platform} verified target differs")
        verified.unlink()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ReleaseRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        value = urllib.parse.urlsplit(newurl)
        require(value.scheme == "https" and value.netloc in {
            "github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"
        }, "unexpected Release download redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_public(url, *, release=False):
    request = urllib.request.Request(url, headers={"Accept-Encoding": "identity", "Cache-Control": "no-cache"})
    return urllib.request.build_opener(ReleaseRedirect() if release else NoRedirect()).open(request, timeout=25)


def read_marker():
    try:
        with open_public(BASE + MARKER) as response:
            require(response.status == 200, "unexpected Pages marker status")
            data = response.read(JSON_LIMIT + 1)
            require(len(data) <= JSON_LIMIT, "oversized Pages marker")
            return data
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def first_pages_is_empty():
    for path in ("", *(f"preview/{p}/{script}" for p, script in PLATFORMS.items()),
                 *(f"preview/{p}/metadata/timestamp.json" for p in PLATFORMS)):
        try:
            with open_public(BASE + path):
                return False
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
    return True


def decide_pages(plan, live, is_empty, verify_progress):
    previous = plan.data["previous_manifest_sha256"]
    if live == plan.marker:
        return False
    if live is None:
        require(previous is None and is_empty(), "missing marker on a non-empty or previously published site")
        return True
    marker = load_json(live)
    require(isinstance(marker, dict) and set(marker) == {"tag", "manifest_sha256", "root_sha256", "pages"} and
            previous is not None and marker["manifest_sha256"] == previous and marker["tag"] != plan.tag,
            "live Pages does not match the approved predecessor")
    require(marker["root_sha256"] == plan.data["root_sha256"], "bootstrap root changed; use a reviewed root transition")
    site_files(marker["pages"])
    for path, old in marker["pages"].items():
        new = plan.data["pages"].get(path)
        mutable = path == "index.html" or path.endswith(("/install.sh", "/install.ps1", "/metadata/timestamp.json"))
        require(new is not None and (mutable or new == old), f"previous Pages file lost or replaced: {path}")
    verify_progress(marker["pages"])
    return True


def timestamp_version(raw):
    value = load_json(raw)["signed"]
    version = value["version"]
    require(value["_type"] == "timestamp" and type(version) is int and version > 0, "invalid timestamp")
    return version


def live_pinned_json(path, expected):
    require(expected["bytes"] <= JSON_LIMIT, f"oversized prior metadata: {path}")
    with open_public(BASE + path) as response:
        raw = response.read(expected["bytes"] + 1)
        require(response.status == 200 and {"bytes": len(raw), "sha256": sha(raw)} == expected,
                f"live metadata does not match the predecessor: {path}")
    return raw


def timestamp_path(platform):
    return f"preview/{platform}/metadata/timestamp.json"


def original_timestamp_path(platform):
    return f"preview/{platform}/approved-timestamp.json"


def public_document(path, *, missing=False):
    try:
        with open_public(BASE + path) as response:
            require(response.status == 200, f"unexpected response for {path}")
            raw = response.read(JSON_LIMIT + 1)
            require(len(raw) <= JSON_LIMIT, f"oversized Pages document: {path}")
            return raw
    except urllib.error.HTTPError as error:
        if missing and error.code == 404:
            error.close()
            return None
        raise


def approved_live_plan(marker, raw, digest):
    require(marker is not None and raw is not None, "no approved Pages publication")
    head = load_json(marker)
    require(isinstance(head, dict), "invalid publication marker")
    plan = read_plan(raw, head.get("tag"), digest)
    require(marker == plan.marker, "live approval differs from the approved manifest")
    return plan


def freshness_pins(plan, record_raw):
    pages = dict(plan.data["pages"])
    if record_raw is not None:
        record = load_json(record_raw)
        require(isinstance(record, dict) and set(record) == {"schema", "manifest_sha256", "timestamps"}
                and type(record["schema"]) is int and record["schema"] == 1
                and record["manifest_sha256"] == plan.digest, "timestamp record belongs to a different approval")
        rows = record["timestamps"]
        require(isinstance(rows, dict) and rows.keys() == {timestamp_path(p) for p in PLATFORMS},
                "timestamp record must include all three platforms")
        for path, row in rows.items():
            require(isinstance(row, dict) and set(row) == {"bytes", "sha256", "version", "expires"},
                    f"invalid timestamp record entry: {path}")
            file = {name: row[name] for name in ("bytes", "sha256")}
            require(valid_pin(file) and file["bytes"] <= JSON_LIMIT, f"invalid timestamp pin: {path}")
            pages[path] = file
    return pages


def metadata(raw, role):
    from tuf.api.metadata import Metadata
    load_json(raw)  # Reject ambiguous JSON before TUF parses the same bytes.
    value = Metadata.from_bytes(raw)
    require(value.signed.type == role and type(value.signed.version) is int and value.signed.version > 0,
            f"wrong or invalid metadata role: {role}")
    return value


def timestamp_row(raw, value):
    return {"bytes": len(raw), "sha256": sha(raw), "version": value.signed.version,
            "expires": value.signed.expires.isoformat()}


@dataclass(frozen=True)
class TimestampState:
    current: object
    root: object
    upstream_expires: datetime
    raw: bytes


def referenced_metadata(read, pages, prefix, role, reference):
    path = prefix + f"{reference.version}.{role}.json"
    require(path in pages and reference.length == pages[path]["bytes"]
            and reference.hashes == {"sha256": pages[path]["sha256"]}, f"{role} differs from approval")
    raw = read(path, pages[path])
    reference.verify_length_and_hashes(raw)
    value = metadata(raw, role)
    require(value.signed.version == reference.version, f"wrong {role} version")
    return value


def check_timestamp_state(plan, record_raw, read, *, at, upstream_fresh):
    """Verify approval, exact snapshot lineage and the current timestamp, including after its expiry."""
    from tuf.api.exceptions import RepositoryError

    pages = freshness_pins(plan, record_raw)
    expected_rows = load_json(record_raw)["timestamps"] if record_raw is not None else {}
    states = {}
    for platform in PLATFORMS:
        try:
            prefix = f"preview/{platform}/metadata/"
            path = timestamp_path(platform)
            root_pin = plan.data["pages"].get(prefix + "1.root.json")
            require(root_pin is not None and root_pin["sha256"] == plan.data["root_sha256"],
                    "public root does not match the approved root")
            trusted = metadata(read(prefix + "1.root.json", root_pin), "root")
            require(trusted.signed.version == 1 and trusted.signed.consistent_snapshot is True,
                    "expected the approved initial consistent-snapshot root")
            verify = trusted.signed.verify_delegate
            verify("root", trusted.signed_bytes, trusted.signatures)
            approved = metadata(read(original_timestamp_path(platform), plan.data["pages"][path]), "timestamp")
            live_raw = read(path, pages[path])
            live = metadata(live_raw, "timestamp")
            verify("timestamp", approved.signed_bytes, approved.signatures)
            verify("timestamp", live.signed_bytes, live.signatures)
            require(live.signed.snapshot_meta.to_dict() == approved.signed.snapshot_meta.to_dict(),
                    "timestamp selects a different snapshot")
            if record_raw is not None:
                require(live.signed.version > approved.signed.version
                        and expected_rows[path] == timestamp_row(live_raw, live),
                        "timestamp version or record differs")
            snapshot = referenced_metadata(read, pages, prefix, "snapshot", approved.signed.snapshot_meta)
            verify("snapshot", snapshot.signed_bytes, snapshot.signatures)
            targets = referenced_metadata(read, pages, prefix, "targets", snapshot.signed.meta["targets.json"])
            verify("targets", targets.signed_bytes, targets.signatures)
            selected = targets.signed.targets[plan.data["platforms"][platform]["target"]]
            zip_path = plan.platform_zip(platform)
            zip_pin = plan.data["pages"][zip_path]
            require(selected.length == zip_pin["bytes"] and selected.hashes == {"sha256": zip_pin["sha256"]},
                    "targets selects a different ZIP")
            upstream_expires = min(trusted.signed.expires, snapshot.signed.expires, targets.signed.expires)
            require(not upstream_fresh or at < upstream_expires, "root, snapshot or targets has expired")
            states[path] = TimestampState(live, trusted, upstream_expires, live_raw)
        except (Stop, ValueError, KeyError, TypeError, RepositoryError) as error:
            raise Stop(f"{platform}: {error}") from error
    return states


def live_freshness(marker, digest, *, upstream_fresh=True, require_approval=False):
    record = public_document(FRESHNESS, missing=True)
    raw = public_document(APPROVAL, missing=True) if record is not None or require_approval else None
    if record is None:
        if require_approval:
            approved_live_plan(marker, raw, digest)
        return record, {}, None
    plan = approved_live_plan(marker, raw, digest)
    states = check_timestamp_state(plan, record, live_pinned_json, at=datetime.now(timezone.utc),
                                   upstream_fresh=upstream_fresh)
    return record, {path: state.raw for path, state in states.items()}, freshness_pins(plan, record)


def verify_retained_platforms(plan, old_pages):
    for platform, cfg in plan.data["platforms"].items():
        if "retained_zip" in cfg:
            prefix = f"preview/{platform}/"
            before = {p: v for p, v in old_pages.items() if p.startswith(prefix)}
            after = {p: v for p, v in plan.data["pages"].items() if p.startswith(prefix)}
            require(before == after, f"retained platform files changed: {platform}")


def verify_timestamp_progress(old_pages, payload):
    for platform in PLATFORMS:
        path = f"preview/{platform}/metadata/timestamp.json"
        raw = live_pinned_json(path, old_pages[path])
        new = bounded_bytes(payload / path)
        require(new == raw or timestamp_version(new) > timestamp_version(raw),
                f"timestamp version would go backward or be reused: {platform}")
        old_snapshot = load_json(raw)["signed"]["meta"]["snapshot.json"]["version"]
        new_snapshot = load_json(new)["signed"]["meta"]["snapshot.json"]["version"]
        require(type(old_snapshot) is int and type(new_snapshot) is int and
                0 < old_snapshot <= new_snapshot, f"snapshot version would go backward: {platform}")
        parent = f"preview/{platform}/metadata/"
        old_snap_path = parent + f"{old_snapshot}.snapshot.json"
        old_snap = live_pinned_json(old_snap_path, old_pages[old_snap_path])
        new_snap = bounded_bytes(payload / (parent + f"{new_snapshot}.snapshot.json"))
        old_targets = load_json(old_snap)["signed"]["meta"]["targets.json"]["version"]
        new_targets = load_json(new_snap)["signed"]["meta"]["targets.json"]["version"]
        require(type(old_targets) is int and type(new_targets) is int and
                0 < old_targets <= new_targets, f"targets version would go backward: {platform}")


def verify_live(plan):
    return verify_live_marker(plan.tag, plan.digest, sha(plan.marker))


def verify_live_marker(tag, manifest_digest, marker_digest, *, require_approval=False):
    live = read_marker()
    require(live is not None and is_sha(marker_digest) and sha(live) == marker_digest,
            "Pages is not serving the approved publication marker")
    marker = load_json(live)
    require(marker["tag"] == tag and marker["manifest_sha256"] == manifest_digest, "Pages publication identity differs")
    site_files(marker["pages"])
    record, stamps, effective = live_freshness(live, manifest_digest, require_approval=require_approval)
    if require_approval and record is None:
        for platform in PLATFORMS:
            live_pinned_json(original_timestamp_path(platform), marker["pages"][timestamp_path(platform)])
    for path, expected in sorted((effective or marker["pages"]).items()):
        with open_public(BASE + path) as response:
            require(response.status == 200 and pin(response, expected["bytes"]) == expected,
                    f"Pages bytes differ: {path}")
    require(read_marker() == live and public_document(FRESHNESS, missing=True) == record,
            "Pages changed during verification")
    return stamps


def seed_timestamp_baseline(plan, verified_timestamps):
    expected = {APPROVAL: {"bytes": len(plan.raw), "sha256": plan.digest}}
    expected.update({original_timestamp_path(platform): plan.data["pages"][timestamp_path(platform)]
                     for platform in PLATFORMS})
    documents = {path: public_document(path, missing=True) for path in expected}
    require(read_marker() == plan.marker, "Pages changed during timestamp baseline verification")
    if all(raw is None for raw in documents.values()):
        require(not verified_timestamps and public_document(FRESHNESS, missing=True) is None,
                "cannot seed a timestamp baseline over a refresh record")
        return True
    for path, raw in documents.items():
        require(raw is not None and {"bytes": len(raw), "sha256": sha(raw)} == expected[path],
                f"timestamp baseline is partial or differs from approval: {path}")
    return False


def render_pages(plan, payload, work):
    site = work / "site"
    site.mkdir()
    for path in plan.data["pages"]:
        target = site / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(payload / path, target)
    (site / MARKER).write_bytes(plan.marker)
    require(sha(plan.raw) == plan.digest, "missing original approved manifest")
    (site / APPROVAL).write_bytes(plan.raw)
    for platform in PLATFORMS:
        shutil.copyfile(site / timestamp_path(platform), site / original_timestamp_path(platform))


class GitHub:
    def __init__(self):
        require(os.environ.get("GITHUB_ACTIONS") == "true" and os.environ.get("GITHUB_REPOSITORY") == REPO and
                os.environ.get("GITHUB_REF") == "refs/heads/main" and os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch",
                "publishing steps run only in the approved repository's manual workflow")
        self.commit = os.environ.get("GITHUB_SHA", "")
        require(re.fullmatch(r"[0-9a-f]{40}", self.commit), "missing downloads checkout commit")
        self.api_path = f"repos/{REPO}"

    def gh(self, args, *, data=None):
        args = ["gh", "api", "--hostname", "github.com", *args]
        return subprocess.run(args, input=data, stdout=subprocess.PIPE,
                              check=True, timeout=240).stdout

    def api(self, path, *, method=None, data=None, paginate=False):
        args = [path]
        if paginate:
            args += ["--paginate", "--slurp"]
        if method:
            args += ["--method", method]
        if data is not None:
            args += ["--input", "-"]
        return load_json(self.gh(args, data=encode(data) if data is not None else None))

    def release(self, tag):
        pages = self.api(self.api_path + "/releases?per_page=100", paginate=True)
        found = [row for page in pages for row in page if row["tag_name"] == tag]
        require(len(found) <= 1, f"multiple Releases for {tag}")
        return found[0] if found else None

    def assets(self, release):
        pages = self.api(f"{self.api_path}/releases/{release['id']}/assets?per_page=100", paginate=True)
        return [row for page in pages for row in page]

    def download(self, asset, destination, limit):
        require(type(asset["id"]) is int and asset["state"] == "uploaded" and
                0 < asset["size"] <= limit, f"invalid staged asset: {asset['name']}")
        # GitHub's asset API may redirect this authenticated request to its storage.
        command = ["gh", "api", "--hostname", "github.com",
                   f"{self.api_path}/releases/assets/{asset['id']}",
                   "-H", "Accept: application/octet-stream"]
        # Limit the transfer to the advertised size and retain a partial file on failure.
        with destination.open("xb") as output, subprocess.Popen(command, stdout=subprocess.PIPE) as job:
            try:
                result = pin(job.stdout, asset["size"], output)
                require(job.wait(timeout=240) == 0 and result["bytes"] == asset["size"],
                        f"incomplete staged asset: {asset['name']}")
            except BaseException:
                if job.poll() is None:
                    job.kill()
                raise

    def create(self, plan, notes):
        return self.api(f"{self.api_path}/releases", method="POST", data={
            "tag_name": plan.tag, "target_commitish": self.commit, "name": plan.tag,
            "body": notes, "draft": True, "prerelease": True,
        })

    def upload(self, release, name, path):
        url = f"https://uploads.github.com/{self.api_path}/releases/{release['id']}/assets"
        url += "?name=" + urllib.parse.quote(name, safe="")
        return load_json(self.gh([url, "--method", "POST", "--input", str(path),
                                  "-H", "Content-Type: application/octet-stream"]))

    def publish(self, release):
        return self.api(f"{self.api_path}/releases/{release['id']}", method="PATCH",
                        data={"draft": False, "prerelease": True, "make_latest": "false"})

    def require_release_review(self):
        env = self.api(f"{self.api_path}/environments/release-publication")
        require(any(rule["type"] == "required_reviewers" and rule.get("reviewers")
                    for rule in env["protection_rules"]),
                "configure a required reviewer on release-publication before deploying Pages")

    def tag_commit(self, tag):
        rows = self.api(f"{self.api_path}/git/matching-refs/tags/{tag}")
        exact = [row["object"] for row in rows if row["ref"] == f"refs/tags/{tag}"]
        require(len(exact) <= 1, "ambiguous published Git tag")
        if exact:
            require(exact[0]["type"] == "commit", "unexpected non-lightweight release tag")
            return exact[0]["sha"]
        return None


def get_staging(github, tag):
    staging = github.release("staging-" + tag)
    require(staging and staging["draft"], "approved staging Release must be an unpublished draft")
    assets = github.assets(staging)
    require(len(assets) == 2 and {row["name"] for row in assets} == {MANIFEST, ARCHIVE},
            "staging draft must contain exactly the manifest and input archive")
    return {row["name"]: row for row in assets}


def check_asset(asset, name, expected):
    require(asset["name"] == name and asset["state"] == "uploaded" and
            asset["size"] == expected["bytes"] and asset.get("digest") == "sha256:" + expected["sha256"],
            f"Release attachment differs: {name}")


def verify_public_asset(plan, asset, name, expected):
    expected_url = f"https://github.com/{REPO}/releases/download/{plan.tag}/{name}"
    require(asset.get("browser_download_url") == expected_url, f"unexpected Release URL: {name}")
    with open_public(expected_url, release=True) as response:
        require(response.status == 200 and pin(response, expected["bytes"]) == expected,
                f"public Release bytes differ: {name}")


def complete_release(github, plan, payload, verify_pages=verify_live, verify_asset=verify_public_asset):
    notes = bounded_bytes(payload / "RELEASE-NOTES.md").decode("utf-8")
    release = github.release(plan.tag)
    tag_commit = github.tag_commit(plan.tag)
    require(tag_commit is None or tag_commit == github.commit, "existing Git tag points to a different downloads commit")
    if release is None:
        require(tag_commit is None, "Git tag exists without its expected release")
        release = github.create(plan, notes)
    require(release["tag_name"] == plan.tag and release["name"] == plan.tag and
            release["body"] == notes and release["prerelease"] and
            (not release["draft"] or not release.get("immutable")), "existing Release identity or notes differ")
    if release["draft"] and tag_commit is None:
        require(release["target_commitish"] == github.commit, "Release draft targets a different downloads commit")
    assets = github.assets(release)
    by_name = {item["name"]: item for item in assets}
    require(len(assets) == len(by_name) and set(by_name) <= set(plan.release_pins),
            "unexpected or duplicate Release attachments")
    for name, asset in by_name.items():
        check_asset(asset, name, plan.release_pins[name])
    if release["draft"]:
        for name, expected in sorted(plan.release_pins.items()):
            if name not in by_name:
                relative = plan.data["release_zips"].get(name, name)
                by_name[name] = github.upload(release, name, payload / relative)
                check_asset(by_name[name], name, expected)
        verify_pages(plan)
        release = github.publish(release)
        published_assets = github.assets(release)
        by_name = {item["name"]: item for item in published_assets}
        require(len(by_name) == len(published_assets), "duplicate public Release attachments")
        for name, expected in plan.release_pins.items():
            if name in by_name:
                check_asset(by_name[name], name, expected)
    require(not release["draft"] and release["prerelease"] and
            github.tag_commit(plan.tag) == github.commit, "Release publication or tag could not be verified")
    require(set(by_name) == set(plan.release_pins), "public Release is missing attachments")
    for name, expected in sorted(plan.release_pins.items()):
        verify_asset(plan, by_name[name], name, expected)
    print(f"Verified Release ID {release['id']}, tag {plan.tag}, and all public attachments")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("step", choices=("pack", "local", "prepare", "verify", "release"))
    parser.add_argument("--work", type=Path, required=True, help="new work directory")
    parser.add_argument("--manifest", type=Path, help="local check: approved manifest")
    parser.add_argument("--archive", type=Path, help="local check: staged input archive")
    parser.add_argument("--spec", type=Path, help="pack: release identity and platform mappings")
    parser.add_argument("--source", type=Path, help="pack: approved Pages tree plus RELEASE-NOTES.md")
    parser.add_argument("--output", type=Path, help="pack: new directory for the two staging assets")
    args = parser.parse_args()
    pack_fields = (args.spec, args.source, args.output)
    if args.step == "pack":
        require(all(pack_fields) and args.manifest is None and args.archive is None,
                "pack needs --spec, --source, and --output")
        make_input(args.spec, args.source, args.output, args.work)
        return
    require(not any(pack_fields), "pack options apply only to pack")
    tag = os.environ.get("RELEASE_TAG", "")
    digest = os.environ.get("MANIFEST_SHA256", "")
    require(is_tag(tag) and is_sha(digest), "set RELEASE_TAG and the approved MANIFEST_SHA256")
    if args.step == "local":
        require(args.manifest is not None and args.archive is not None, "local needs --manifest and --archive")
        plan = read_plan(bounded_bytes(args.manifest), tag, digest)
        args.work.mkdir()
        payload = unpack(plan, args.archive, args.work)
        verify_trust(plan, payload, args.work)
        render_pages(plan, payload, args.work)
        print(f"Local validation passed for {plan.tag}, {plan.digest}")
        return
    require(args.manifest is None and args.archive is None, "workflow reads inputs from its staging draft")
    if args.step == "verify":
        verify_live_marker(tag, digest, os.environ.get("PAGES_MARKER_SHA256", ""), require_approval=True)
        print("Verified anonymous Pages responses")
        return
    seed = os.environ.get("SEED_TIMESTAMP_BASELINE", "false")
    require(seed in ("true", "false"), "invalid timestamp baseline selection")
    seed = seed == "true"
    if seed:
        require(args.step == "prepare" and tag == "v0.1.1" and os.environ.get("PAGES_ONLY") == "true",
                "timestamp baseline seeding requires v0.1.1 and a Pages-only run")
    github = GitHub()
    args.work.mkdir()
    staging = get_staging(github, tag)
    manifest_path = args.work / MANIFEST
    github.download(staging[MANIFEST], manifest_path, JSON_LIMIT)
    plan = read_plan(bounded_bytes(manifest_path), tag, digest)
    print(f"Approved publication: {plan.tag}, {plan.digest}, source {plan.data['source_commit']}")
    timestamps = verify_live(plan) if args.step == "release" else None
    archive_path = args.work / ARCHIVE
    github.download(staging[ARCHIVE], archive_path, LIMIT)
    payload = unpack(plan, archive_path, args.work)
    if args.step == "prepare":
        live = read_marker()
        if seed:
            require(live == plan.marker, "live Pages does not match the approved v0.1.1 publication")
        timestamps = verify_live(plan) if live == plan.marker else None
        verify_trust(plan, payload, args.work, timestamps=timestamps)
        review_requested = os.environ.get("REVIEW_INSTALLATION", "false")
        require(review_requested in ("true", "false"), "invalid installation review selection")
        review = plan.data["previous_manifest_sha256"] is None or review_requested == "true"
        if review:
            github.require_release_review()
        def verify_predecessor(old):
            previous = plan.data["previous_manifest_sha256"]
            record, _, effective = live_freshness(live, previous, upstream_fresh=False)
            verify_retained_platforms(plan, effective or old)
            verify_timestamp_progress(effective or old, payload)
            require(read_marker() == live and public_document(FRESHNESS, missing=True) == record,
                    "Pages changed during predecessor verification")

        if seed:
            deploy = seed_timestamp_baseline(plan, timestamps)
            print("Adding the four approved timestamp baseline files" if deploy
                  else "Approved timestamp baseline already live; skipping deployment")
        else:
            deploy = decide_pages(plan, live, first_pages_is_empty, verify_predecessor)
            if not deploy:
                print("Approved Pages snapshot already live; skipping deployment")
        if deploy:
            render_pages(plan, payload, args.work)
        with open(os.environ["GITHUB_OUTPUT"], "a") as output:
            output.write(f"deploy={str(deploy).lower()}\nmarker_sha256={sha(plan.marker)}\n")
            output.write(f"environment={'release-publication' if review else 'preview-release'}\n")
    else:
        verify_trust(plan, payload, args.work, timestamps=timestamps)
        complete_release(github, plan, payload)


if __name__ == "__main__":
    try:
        main()
    except (Stop, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, zipfile.BadZipFile) as error:
        print(f"Publication stopped: {error}", file=sys.stderr)
        sys.exit(1)
