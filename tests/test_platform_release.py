import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import publish


def manifest():
    pin = {"bytes": 1, "sha256": "a" * 64}
    pages = {"index.html": pin}
    platforms = {}
    attachments = {}
    for platform, script in publish.PLATFORMS.items():
        prefix = f"preview/{platform}/"
        target = f"colvia-{platform}.zip"
        bootstrap = prefix + "bootstrap/" + pin["sha256"] + ".zip"
        selected = prefix + "targets/" + pin["sha256"] + "." + target
        for path in (prefix + script, bootstrap, prefix + "metadata/timestamp.json", selected):
            pages[path] = pin
        platforms[platform] = {"bootstrap": bootstrap, "target": target}
        attachments[f"colvia-v0.6.1-{platform}.zip"] = selected
    return dict(schema=1, tag="v0.6.1", channel=publish.CHANNEL, source_commit="b" * 40,
                base_url=publish.BASE, previous_manifest_sha256="c" * 64, root_sha256="d" * 64,
                pages=pages, platforms=platforms, release_zips=attachments, checksums=pin, notes=pin)


def partial():
    data = manifest()
    data["schema"] = 2
    for platform in ("macos-arm64", "windows-arm64"):
        path = data["release_zips"].pop(f"colvia-v0.6.1-{platform}.zip")
        data["platforms"][platform]["retained_zip"] = path
    return data


def parse(data):
    raw = publish.encode(data)
    return publish.read_plan(raw, data["tag"], publish.sha(raw))


class PlatformRelease(unittest.TestCase):
    def test_full_release(self):
        plan = parse(manifest())
        self.assertEqual(len(plan.release_pins), 4)
        for platform in publish.PLATFORMS:
            self.assertIn(plan.platform_zip(platform), plan.archive_pins)

    def test_partial_release(self):
        plan = parse(partial())
        self.assertEqual(set(plan.release_pins), {"colvia-v0.6.1-windows-x64.zip", "SHA256SUMS.txt"})
        for platform in publish.PLATFORMS:
            self.assertTrue(plan.platform_zip(platform).startswith(f"preview/{platform}/targets/"))
        publish.verify_retained_platforms(plan, plan.data["pages"])

    def test_invalid_release_scope(self):
        for case in ("legacy", "extra-attachment", "no-predecessor", "wrong-platform", "empty"):
            with self.subTest(case=case):
                data = partial()
                cfg = data["platforms"]["macos-arm64"]
                if case == "legacy":
                    data["schema"] = 1
                elif case == "extra-attachment":
                    data["release_zips"]["colvia-v0.6.1-macos-arm64.zip"] = cfg["retained_zip"]
                elif case == "no-predecessor":
                    data["previous_manifest_sha256"] = None
                elif case == "wrong-platform":
                    cfg["retained_zip"] = next(iter(data["release_zips"].values()))
                else:
                    data["release_zips"].clear()
                with self.assertRaises(publish.Stop):
                    parse(data)

    def test_retained_paths_equal_effective_predecessor(self):
        plan = parse(partial())
        for path in ("preview/windows-arm64/install.ps1", "preview/macos-arm64/metadata/timestamp.json"):
            with self.subTest(path=path):
                live = copy.deepcopy(plan.data["pages"])
                live[path] = {"bytes": 2, "sha256": "e" * 64}
                with self.assertRaises(publish.Stop):
                    publish.verify_retained_platforms(plan, live)
