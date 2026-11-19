import importlib.util
import io
import json
import pathlib
import sys
import unittest
from unittest.mock import patch
import zipfile

path = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "sync_gitee_releases.py"
spec = importlib.util.spec_from_file_location("mirror", path)
mirror = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mirror
spec.loader.exec_module(mirror)


def jar(mod="bankmod", version="1.0.19", env="*"):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("fabric.mod.json", json.dumps({"id": mod, "version": version, "environment": env}))
    return out.getvalue()


def release(data, **kw):
    values = {"tag_name": "v1.0.19", "draft": False, "prerelease": False,
              "published_at": "2026-11-19T11:00:00Z", "assets": [
                  {"name": "bankmod-1.0.19-sources.jar"},
                  {"name": "bankmod-1.0.19.jar", "state": "uploaded", "size": len(data),
                   "digest": "sha256:" + mirror.sha(data),
                   "browser_download_url": "https://github.com/heplus1s/MinecraftBankMod/releases/download/v1.0.19/bankmod-1.0.19.jar"}]}
    return values | kw


class MirrorTests(unittest.TestCase):
    def setUp(self):
        self.data = jar()
        self.candidate = mirror.candidate_for("bankmod", release(self.data))

    @patch.object(mirror, "json_request")
    def test_metadata_uses_action_token_only_on_github_api(self, request):
        request.return_value = release(self.data)
        with patch.dict(mirror.os.environ, {"GH_TOKEN": "test-runner-token"}):
            self.assertEqual(self.candidate, mirror.get_github("bankmod", "v1.0.19"))
        url = request.call_args.args[0]
        self.assertEqual("https://api.github.com/repos/heplus1s/MinecraftBankMod/releases/tags/v1.0.19", url)
        self.assertEqual({"api.github.com"}, request.call_args.kwargs["hosts"])
        self.assertEqual("Bearer test-runner-token", request.call_args.kwargs["headers"]["Authorization"])
        self.assertFalse(request.call_args.kwargs.get("redirects", False))

    @patch.object(mirror, "json_request", side_effect=mirror.HttpStatus(403))
    def test_metadata_403_identifies_stage(self, request):
        with self.assertRaisesRegex(mirror.SyncError, "GitHub Release API returned HTTP 403"):
            mirror.get_github("bankmod", "v1.0.19")

    def test_exact_asset_and_no_toll(self):
        self.assertEqual(self.candidate.file, "bankmod-1.0.19.jar")
        self.assertEqual(self.candidate.gitee_tag, "bankmod-v1.0.19")
        self.assertNotIn("tollmod", mirror.MODS)
        mirror.inspect_jar(self.candidate, self.data)

    def test_reject_draft_prerelease_and_duplicate(self):
        for params in ({"draft": True}, {"prerelease": True}, {"tag_name": "v1.0.19-beta"}):
            with self.assertRaises(mirror.SyncError):
                mirror.candidate_for("bankmod", release(self.data, **params))
        assets = release(self.data)["assets"]
        with self.assertRaises(mirror.SyncError):
            mirror.candidate_for("bankmod", release(self.data, assets=[assets[1], assets[1]]))

    def test_reject_hash_environment_version(self):
        for data in (jar("tollmod"), jar(version="1.0.18"), jar(env="server")):
            with self.assertRaises(mirror.SyncError):
                mirror.inspect_jar(mirror.candidate_for("bankmod", release(data)), data)
        with self.assertRaises(mirror.SyncError):
            mirror.inspect_jar(self.candidate, self.data + b"x")

    @patch.object(mirror, "find_release")
    @patch.object(mirror, "request")
    def test_dry_run_has_no_gitee_call(self, request, find_release):
        request.return_value = self.data
        mirror.copy(self.candidate, "unused", False)
        find_release.assert_not_called()
        request.assert_called_once_with(self.candidate.url, hosts=mirror.GH_HOSTS,
                                        limit=mirror.MAX_JAR, redirects=True)

    @patch.object(mirror, "gitee_download")
    @patch.object(mirror, "gitee_upload")
    @patch.object(mirror, "gitee_read")
    @patch.object(mirror, "gitee_create")
    @patch.object(mirror, "find_release")
    @patch.object(mirror, "request")
    def test_existing_is_never_uploaded(self, request, find_release, create, read, upload, download):
        request.return_value = self.data
        find_release.return_value = {"id": 123, "tag_name": self.candidate.gitee_tag}
        read.return_value = [{"name": self.candidate.file}]
        mirror.copy(self.candidate, "token", True)
        create.assert_not_called()
        upload.assert_not_called()
        download.assert_called_once_with(self.candidate, mirror.sha(self.data))

    @patch.object(mirror, "gitee_download")
    @patch.object(mirror, "gitee_upload")
    @patch.object(mirror, "gitee_read")
    @patch.object(mirror, "gitee_create")
    @patch.object(mirror, "find_release")
    @patch.object(mirror, "request")
    def test_new_attachment_upload_then_public_verify(self, request, find_release, create, read, upload, download):
        request.return_value = self.data
        find_release.return_value = None
        create.return_value = {"id": 123, "tag_name": self.candidate.gitee_tag}
        read.return_value = []
        order = []
        upload.side_effect = lambda *_: order.append("upload")
        download.side_effect = lambda *_: order.append("verify")
        mirror.copy(self.candidate, "token", True)
        self.assertEqual(order, ["upload", "verify"])

    @patch.object(mirror, "gitee_download")
    @patch.object(mirror, "gitee_upload", side_effect=mirror.SyncError("Network request failed (TimeoutError)"))
    @patch.object(mirror, "list_attachments")
    @patch.object(mirror, "find_release")
    @patch.object(mirror, "request")
    def test_lost_upload_response_checks_read_only_without_reupload(self, request, find_release, listing, upload, download):
        request.return_value = self.data
        find_release.return_value = {"id": 123, "tag_name": self.candidate.gitee_tag}
        listing.side_effect = [[], [{"name": self.candidate.file}]]
        mirror.copy(self.candidate, "token", True)
        upload.assert_called_once()
        download.assert_called_once()

    @patch.object(mirror, "request")
    def test_public_copy_refuses_different_bytes(self, request):
        request.return_value = b"not the upstream jar"
        with self.assertRaisesRegex(mirror.SyncError, "nothing overwritten"):
            mirror.gitee_download(self.candidate, mirror.sha(self.data))

    def test_verified_gitee_cdn_is_exact_https_host(self):
        self.assertTrue(mirror.endpoint_ok("https://foruda.gitee.com/file.jar", mirror.GITEE_HOSTS))
        self.assertFalse(mirror.endpoint_ok("https://foruda.gitee.com.evil.invalid/file.jar", mirror.GITEE_HOSTS))
        self.assertFalse(mirror.endpoint_ok("http://foruda.gitee.com/file.jar", mirror.GITEE_HOSTS))
        self.assertFalse(mirror.endpoint_ok("https://user@foruda.gitee.com/file.jar", mirror.GITEE_HOSTS))

    def test_possible_credential_redirects_fail(self):
        self.assertFalse(mirror.endpoint_ok("https://api.github.com@other.invalid", mirror.GH_HOSTS))
        self.assertFalse(mirror.endpoint_ok("http://api.github.com", mirror.GH_HOSTS))
        self.assertFalse(mirror.endpoint_ok("https://gitee.com:444/x", mirror.GITEE_HOSTS))
        with self.assertRaises(mirror.SyncError):
            mirror.SafeRedirect({"gitee.com"}, False).redirect_request(
                None, None, 307, "", None, "https://gitee.com/elsewhere")

    def manifests(self):
        rows = [{"id": mod, "version": "1.0.18", "file": f"{mod}-1.0.18.jar",
                 "bytes": 3, "sha256": "a" * 64, "url": f"https://gitee.com/original/{mod}",
                 "keepRowMetadata": True} for mod in mirror.MODS]
        raw = {"schema": 1, "packId": "ncepu-hd", "minecraft": "1.20.1", "release": "v3.04",
               "kind": "latest", "mods": rows, "keepTopLevelMetadata": True}
        install = {**raw, "kind": "install", "assetSet": "unverified",
                   "mods": rows + [{"id": "customskinloader", "url": None, "pclFallback": "example"}]}
        return raw, install

    def replacements(self):
        c = self.candidate
        return {c.mod: {"id": c.mod, "version": c.version, "file": c.file,
                        "bytes": c.size, "sha256": mirror.sha(self.data), "url": c.gitee_url}}

    def test_latest_updates_only_approved_row_and_preserves_v304(self):
        latest, _ = self.manifests()
        out = mirror.patch_manifest(latest, self.replacements())
        self.assertEqual("v3.04", out["release"])
        self.assertEqual("1.0.18", latest["mods"][0]["version"])
        self.assertEqual("1.0.19", out["mods"][0]["version"])
        self.assertTrue(out["mods"][0]["keepRowMetadata"])
        self.assertEqual(latest["mods"][1:], out["mods"][1:])
        self.assertTrue(out["keepTopLevelMetadata"])

    def test_older_versions_cannot_auto_downgrade(self):
        latest, _ = self.manifests()
        new = self.replacements()
        new["bankmod"]["version"] = "1.0.1"
        self.assertEqual(latest, mirror.patch_manifest(latest, new))

    def test_same_version_mutating_bytes_rejected(self):
        latest, _ = self.manifests()
        new = self.replacements()
        new["bankmod"]["version"] = "1.0.18"
        with self.assertRaises(mirror.SyncError):
            mirror.patch_manifest(latest, new)

    def test_install_checksum_is_fail_closed(self):
        _, install = self.manifests()
        with self.assertRaisesRegex(mirror.SyncError, "Cannot verify"):
            mirror.patch_manifest(install, self.replacements(), "canonical-json-sha256")
        out = mirror.patch_manifest(install, self.replacements(), "preserve")
        self.assertEqual(install["assetSet"], out["assetSet"])
        self.assertEqual(install["mods"][-1], out["mods"][-1])
        install["assetSet"] = mirror.manifest_fingerprint(install["mods"])
        out = mirror.patch_manifest(install, self.replacements(), "canonical-json-sha256")
        self.assertEqual(mirror.manifest_fingerprint(out["mods"]), out["assetSet"])
        self.assertNotEqual(install["assetSet"], out["assetSet"])

    def test_duplicate_and_unapproved_id_rejected_in_latest(self):
        latest, _ = self.manifests()
        latest["mods"].append({"id": "tollmod"})
        with self.assertRaises(mirror.SyncError):
            mirror.safely_read_manifest(json.dumps(latest), "latest")
        with self.assertRaises(mirror.SyncError):
            mirror.safely_read_manifest('{"schema":1,"schema":1}', "latest")

    def test_publish_latest_only_never_stages_install_or_forces(self):
        latest, _ = self.manifests()
        calls = []
        def fake_git(where, args, extra_env=None):
            calls.append(args)
            if args[0] == "clone":
                (where / "repo").mkdir()
            if args[0] == "show":
                self.assertEqual(args, ["show", "HEAD:latest.json"])
                return json.dumps(latest).encode()
            if args[0] == "diff":
                return b"latest.json\0"
            if args[0] == "push":
                self.assertEqual(args, ["push", "origin", "HEAD:refs/heads/master"])
                self.assertTrue((where / "latest.json").is_file())
                self.assertFalse((where / "install.json").exists())
            return b""
        with patch.object(mirror, "git", side_effect=fake_git), \
             patch.object(mirror, "confirm_public_manifests") as confirm, \
             patch.dict(mirror.os.environ, {"GITEE_TOKEN": "test-token"}):
            mirror.publish_manifests(self.replacements(), "latest")
            self.assertEqual({"latest.json"}, set(confirm.call_args.args[0]))
        self.assertEqual(sum(x[0] == "push" for x in calls), 1)

    def test_public_raw_must_match_exact_new_json(self):
        old, _ = self.manifests()
        expected = mirror.patch_manifest(old, self.replacements())
        with patch.object(mirror, "request", side_effect=[json.dumps(old), json.dumps(expected)]) as fetch, \
             patch.object(mirror.time, "sleep"):
            mirror.confirm_public_manifests({"latest.json": expected})
            self.assertEqual(2, fetch.call_count)
            self.assertEqual({"gitee.com", "raw.giteeusercontent.com"}, fetch.call_args.kwargs["hosts"])
            self.assertTrue(fetch.call_args.kwargs["redirects"])
            self.assertNotIn("Authorization", fetch.call_args.kwargs["headers"])
            self.assertFalse(mirror.endpoint_ok("https://raw.giteeusercontent.com.evil.invalid/file", mirror.GITEE_RAW_HOSTS))


if __name__ == "__main__":
    unittest.main()
