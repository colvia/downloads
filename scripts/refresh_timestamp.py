"""Refresh the three TUF timestamps of one previously approved Pages snapshot."""
import argparse
import base64
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import signal
import sys
import time
from urllib.error import HTTPError

import publish as pub

TIMESTAMP_DAYS = 14
WARNING_DAYS = 21
VISIBILITY_LIMIT_SECONDS = 120
VISIBILITY_POLL_SECONDS = 5
PREVIOUS_RECORD = "previous-timestamp-publication.json"


def require_actions():
    pub.require(os.environ.get("GITHUB_ACTIONS") == "true"
                and os.environ.get("GITHUB_REPOSITORY") == pub.REPO
                and os.environ.get("GITHUB_REF") == "refs/heads/main"
                and os.environ.get("GITHUB_EVENT_NAME") in {"schedule", "workflow_dispatch"},
                "timestamp refresh requires the downloads repository's main workflow")


def load_signer():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from securesystemslib.signer import CryptoSigner

    encoded = os.environ.pop("COLVIA_TIMESTAMP_KEY_B64", "")
    try:
        raw = base64.b64decode(encoded, validate=True)
        return CryptoSigner(Ed25519PrivateKey.from_private_bytes(raw))
    except (TypeError, ValueError):
        raise pub.Stop("timestamp signing secret is missing or invalid") from None


def download(path, pin, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    with pub.open_public(pub.BASE + path) as response, destination.open("xb") as output:
        pub.require(response.status == 200 and pub.pin(response, pin["bytes"], output) == pin,
                    f"live file differs from approval or refresh record: {path}")


def ensure_publication_is_current(marker, record):
    pub.require(pub.read_marker() == marker and pub.public_document(pub.FRESHNESS, missing=True) == record,
                "Pages changed while preparing the refresh")


def prepare(work, approval_digest, signer_factory, *, at):
    from tuf.api.metadata import Metadata, Timestamp

    pub.require(pub.is_sha(approval_digest), "missing approved manifest SHA-256")
    marker = pub.read_marker()
    approval = pub.public_document(pub.APPROVAL)
    plan = pub.approved_live_plan(marker, approval, approval_digest)
    current_record = pub.public_document(pub.FRESHNESS, missing=True)
    effective = pub.freshness_pins(plan, current_record)

    work.mkdir()
    staged = work / "checked"
    staged.mkdir()
    timestamps = {pub.timestamp_path(p) for p in pub.PLATFORMS}
    approved_originals = {pub.original_timestamp_path(p): plan.data["pages"][pub.timestamp_path(p)]
                          for p in pub.PLATFORMS}
    immutable = {path: pin for path, pin in effective.items() if path not in timestamps}
    for path, pin in (effective | approved_originals).items():
        download(path, pin, staged / path)
    (staged / pub.APPROVAL).write_bytes(approval)
    (staged / pub.MARKER).write_bytes(marker)

    def local_read(path, pin):
        data = pub.bounded_bytes(staged / path, pin["bytes"])
        pub.require({"bytes": len(data), "sha256": pub.sha(data)} == pin,
                    f"local staged bytes differ: {path}")
        return data

    states = pub.check_timestamp_state(plan, current_record, local_read, at=at, upstream_fresh=True)
    ensure_publication_is_current(marker, current_record)
    signer = signer_factory()
    for state in states.values():
        key = signer.public_key
        root = state.root.signed
        pub.require(key.keyid in root.roles["timestamp"].keyids
                    and root.keys[key.keyid].to_dict() == key.to_dict(),
                    "timestamp signing key does not match the approved delegation")

    replacements = {}
    rows = {}
    expires = at + timedelta(days=TIMESTAMP_DAYS)
    for path, state in states.items():
        timestamp = Metadata(Timestamp(version=state.current.signed.version + 1, expires=expires,
                                       snapshot_meta=state.current.signed.snapshot_meta))
        timestamp.sign(signer)
        state.root.signed.verify_delegate("timestamp", timestamp.signed_bytes, timestamp.signatures)
        replacements[path] = timestamp.to_bytes()
        rows[path] = pub.timestamp_row(replacements[path], timestamp)

    next_record = pub.encode({"schema": 1, "manifest_sha256": plan.digest, "timestamps": rows})
    next_pins = pub.freshness_pins(plan, next_record)
    pub.require(set(next_pins) == set(effective) and all(next_pins[path] == pin for path, pin in immutable.items()),
                "weekly operation would change an approved file")
    for path, raw in replacements.items():
        (staged / path).write_bytes(raw)
    (staged / pub.FRESHNESS).write_bytes(next_record)
    expected_files = set(next_pins) | set(approved_originals) | {pub.APPROVAL, pub.MARKER, pub.FRESHNESS}
    members = list(staged.rglob("*"))
    pub.require(all(not p.is_symlink() and (p.is_file() or p.is_dir()) for p in members)
                and {p.relative_to(staged).as_posix() for p in members if p.is_file()} == expected_files,
                "unexpected Pages output path")
    for path, pin in (next_pins | approved_originals).items():
        pub.require(pub.file_pin(staged / path, pin["bytes"]) == pin, f"staged Pages bytes differ: {path}")
    pub.check_timestamp_state(plan, next_record, local_read, at=at, upstream_fresh=True)
    ensure_publication_is_current(marker, current_record)

    # Keep the verified predecessor outside the upload directory for the post-deploy check.
    (work / PREVIOUS_RECORD).write_bytes(current_record or b"")
    # This is the sole directory the Pages upload step may use.
    staged.rename(work / "site")
    return min(state.upstream_expires for state in states.values())


def verify(work, approval_digest, *, at):
    site = work / "site"
    marker = pub.bounded_bytes(site / pub.MARKER)
    plan = pub.approved_live_plan(marker, pub.bounded_bytes(site / pub.APPROVAL), approval_digest)
    expected_record = pub.bounded_bytes(site / pub.FRESHNESS)
    previous_record = pub.bounded_bytes(work / PREVIOUS_RECORD) or None
    pub.require(pub.public_document(pub.APPROVAL) == plan.raw, f"{pub.APPROVAL}: public manifest differs")

    def pin(raw):
        return {"bytes": len(raw), "sha256": pub.sha(raw)}

    paths = tuple(pub.timestamp_path(platform) for platform in pub.PLATFORMS)
    old_pins = pub.freshness_pins(plan, previous_record)
    new_pins = pub.freshness_pins(plan, expected_record)
    old_pins[pub.FRESHNESS] = pin(previous_record) if previous_record is not None else None
    new_pins[pub.FRESHNESS] = pin(expected_record)
    deadline = time.monotonic() + VISIBILITY_LIMIT_SECONDS
    pending = {}
    last_pending = {}
    reading_path = None

    def timed_out(_signum=None, _frame=None):
        detail = last_pending | pending
        if reading_path is not None:
            detail[reading_path] = "HTTPS read still in progress"
        items = "; ".join(f"{path}: {status}" for path, status in detail.items())
        raise pub.Stop(f"Pages visibility timed out after {VISIBILITY_LIMIT_SECONDS}s"
                       + (f" ({items})" if items else ""))

    def remaining():
        left = deadline - time.monotonic()
        if left <= 0:
            timed_out()
        return left

    def read_document(path):
        nonlocal reading_path
        remaining()
        reading_path = path
        try:
            raw = pub.public_document(path)
        finally:
            reading_path = None
        remaining()
        return raw

    def check_marker():
        try:
            pub.require(read_document(pub.MARKER) == marker, f"{pub.MARKER}: publication marker differs")
        except HTTPError as error:
            error.close()
            raise pub.Stop(f"{pub.MARKER}: HTTP {error.code}") from error

    def read_mutable(path):
        try:
            raw = read_document(path)
        except HTTPError as error:
            error.close()
            if error.code == 503 or (error.code == 404 and path == pub.FRESHNESS and previous_record is None):
                pending[path] = f"HTTP {error.code}"
                return None
            raise pub.Stop(f"{path}: HTTP {error.code}") from error
        observed = pin(raw)
        if observed == new_pins[path]:
            return raw
        if observed != old_pins[path]:
            raise pub.Stop(f"{path}: public bytes match neither the prepared update nor its predecessor")
        pending[path] = "previous publication still visible"
        return None

    # Bound the wait even when an HTTPS read is still in progress on the Ubuntu Actions runner.
    old_handler = signal.signal(signal.SIGALRM, timed_out)
    try:
        signal.setitimer(signal.ITIMER_REAL, remaining())
        while True:
            pending = {}
            check_marker()
            read_mutable(pub.FRESHNESS)
            visible = {path: read_mutable(path) for path in paths}
            if not pending:
                def read_pinned(path, expected):
                    if path in visible:
                        return visible[path]
                    return pub.live_pinned_json(path, expected)

                states = pub.check_timestamp_state(plan, expected_record, read_pinned, at=at, upstream_fresh=True)
                pub.require(all(state.current.signed.expires > at for state in states.values()),
                            "public timestamp has expired")
                check_marker()
                read_mutable(pub.FRESHNESS)
                if not pending:
                    return min(state.upstream_expires for state in states.values())
            if pending != last_pending:
                detail = "; ".join(f"{path}: {status}" for path, status in pending.items())
                print(f"Waiting for Pages visibility ({detail})", flush=True)
            last_pending = pending
            time.sleep(min(VISIBILITY_POLL_SECONDS, remaining()))
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("step", choices=("prepare", "verify"))
    parser.add_argument("--work", type=Path, required=True)
    args = parser.parse_args()
    require_actions()
    digest = os.environ.get("COLVIA_APPROVED_MANIFEST_SHA256", "")
    now = datetime.now(timezone.utc).replace(microsecond=0)
    if args.step == "prepare":
        expiry = prepare(args.work, digest, load_signer, at=now)
        print(f"Prepared a verified timestamp-only Pages update. Upstream expiry: {expiry.isoformat()}")
        return
    expiry = verify(args.work, digest, at=now)
    message = f"Timestamp refresh verified on all three platforms. Next upstream expiry: {expiry.isoformat()}"
    print(message)
    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as summary:
        print(message, file=summary)
        if expiry - now <= timedelta(days=WARNING_DAYS):
            print("Maintainer action: arrange the offline metadata renewal before this deadline.", file=summary)
    if expiry - now <= timedelta(days=WARNING_DAYS):
        raise pub.Stop("refresh succeeded; upstream metadata expires within 21 days")


if __name__ == "__main__":
    from tuf.api.exceptions import RepositoryError

    try:
        main()
    except (pub.Stop, OSError, ValueError, RepositoryError) as error:
        print(f"Timestamp refresh stopped: {error}", file=sys.stderr)
        sys.exit(1)
