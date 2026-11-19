#!/usr/bin/env python3
"""Copy approved public GitHub JARs to versioned Gitee release attachments only."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import secrets
import sys
import subprocess
import tempfile
from copy import deepcopy
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass

MODS = {
    "bankmod": "MinecraftBankMod",
    "shopmod": "MinecraftShopMod",
    "marketmod": "MinecraftMarketMod",
    "landmod": "MinecraftLandMod",
}
GH_OWNER = "heplus1s"
GITEE_REPO = "intelsayamdyes/mc-client-pack"
GITEE_WEB = "https://gitee.com/" + GITEE_REPO
GITEE_API = "https://gitee.com/api/v5/repos/" + GITEE_REPO
GITEE_RAW_HOSTS = {"gitee.com", "raw.giteeusercontent.com"}
MAX_JAR = 50 * 1024 * 1024
VERSION = re.compile(r"v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
GH_HOSTS = {"api.github.com", "github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}
GITEE_HOSTS = {"gitee.com", "foruda.gitee.com", "gitee.com.cn", "gitee-prod-attachment.oss-cn-beijing.aliyuncs.com"}


class SyncError(Exception):
    pass


class HttpStatus(SyncError):
    def __init__(self, code: int):
        self.code = code
        super().__init__(f"HTTP {code}")


def endpoint_ok(url: str, hosts: set[str]) -> bool:
    parsed = urllib.parse.urlsplit(url)
    return (parsed.scheme == "https" and parsed.hostname in hosts and
            not parsed.username and not parsed.password and parsed.port is None)


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, hosts: set[str], allow: bool):
        self.hosts, self.allow = hosts, allow

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not self.allow or not endpoint_ok(newurl, self.hosts):
            parsed = urllib.parse.urlsplit(newurl)
            host = parsed.hostname or ""
            safe_host = host if re.fullmatch(r"[A-Za-z0-9.-]{1,253}", host) else "unknown"
            raise SyncError(f"Unexpected or credential-bearing redirect (host: {safe_host})")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def request(url, *, hosts, limit=2_000_000, data=None, headers=None, redirects=False, timeout=30):
    if not endpoint_ok(url, hosts):
        raise SyncError("Unapproved endpoint")
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "mc-client-pack-sync/1", **(headers or {})})
    opener = urllib.request.build_opener(SafeRedirect(hosts, redirects))
    try:
        with opener.open(req, timeout=timeout) as response:
            contents = response.read(limit + 1)
    except urllib.error.HTTPError as exc:
        raise HttpStatus(exc.code) from None
    except (OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        # Preserve only the exception type; arguments may include a URL with credentials.
        raise SyncError(f"Network request failed ({type(reason).__name__})") from None
    if len(contents) > limit:
        raise SyncError("Response size exceeds limit")
    return contents


def json_request(url, **kw):
    try:
        return json.loads(request(url, **kw))
    except (ValueError, UnicodeError):
        raise SyncError("Invalid JSON response") from None


def version_of(tag: str) -> str:
    match = VERSION.fullmatch(tag)
    if not match:
        raise SyncError("Release tag is not a stable x.y.z version")
    return ".".join(match.groups())


@dataclass(frozen=True)
class Candidate:
    mod: str
    repo: str
    github_tag: str
    version: str
    file: str
    url: str
    size: int
    digest: str | None

    @property
    def gitee_tag(self):
        return f"{self.mod}-v{self.version}"

    @property
    def gitee_url(self):
        return f"{GITEE_WEB}/releases/download/{self.gitee_tag}/{self.file}"


def candidate_for(mod: str, release: dict) -> Candidate:
    if release.get("draft") is not False or release.get("prerelease") is not False or not release.get("published_at"):
        raise SyncError("Release is not published stable")
    tag = release["tag_name"]
    version = version_of(tag)
    repo = MODS[mod]
    filename = f"{mod}-{version}.jar"
    assets = [item for item in release.get("assets", []) if item.get("name") == filename]
    if len(assets) != 1:
        raise SyncError("Expected exactly one approved JAR attachment")
    asset = assets[0]
    url = f"https://github.com/{GH_OWNER}/{repo}/releases/download/{tag}/{filename}"
    if asset.get("browser_download_url") != url or asset.get("state") != "uploaded":
        raise SyncError("Unexpected source URL or upload state")
    size = asset.get("size")
    if type(size) is not int or not 1 <= size <= MAX_JAR:
        raise SyncError("Attachment size is absent or outside the configured limit")
    digest = asset.get("digest")
    if digest is not None and (not isinstance(digest, str) or not re.fullmatch("sha256:[a-f0-9]{64}", digest)):
        raise SyncError("Unexpected GitHub digest format")
    return Candidate(mod, repo, tag, version, filename, url, size, digest)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def inspect_jar(candidate: Candidate, data: bytes):
    if len(data) != candidate.size or candidate.digest and candidate.digest != "sha256:" + sha(data):
        raise SyncError("Source size or GitHub SHA-256 mismatch")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            if sum(i.file_size for i in z.infolist()) > 250 * 1024 * 1024:
                raise SyncError("Uncompressed archive exceeds limit")
            files = [x for x in z.infolist() if x.filename == "fabric.mod.json"]
            if len(files) != 1 or files[0].file_size > 65_536:
                raise SyncError("Missing, ambiguous, or oversized Fabric metadata")
            info = json.loads(z.read(files[0]))
            if z.testzip() is not None:
                raise SyncError("ZIP CRC check failed")
    except (zipfile.BadZipFile, ValueError, RuntimeError, NotImplementedError):
        raise SyncError("Invalid Fabric JAR") from None
    if info.get("id") != candidate.mod or info.get("version") != candidate.version:
        raise SyncError("Mod ID or version does not match the approved asset")
    if info.get("environment", "*") not in ("*", "client"):
        raise SyncError("Not an approved client-compatible mod")


def get_github(mod, version=None):
    url = f"https://api.github.com/repos/{GH_OWNER}/{MODS[mod]}/releases"
    path = f"/tags/{urllib.parse.quote(version, safe='')}" if version else "/latest"
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    gh_token = os.environ.get("GH_TOKEN", "")
    if gh_token:
        headers["Authorization"] = "Bearer " + gh_token
    try:
        release = json_request(url + path, hosts={"api.github.com"}, headers=headers)
    except HttpStatus as exc:
        raise SyncError(f"GitHub Release API returned HTTP {exc.code}") from None
    return candidate_for(mod, release)


def gitee_read(path, token):
    query = urllib.parse.urlencode({"access_token": token})
    return json_request(f"{GITEE_API}{path}?{query}", hosts={"gitee.com"})


def gitee_create(candidate, token):
    # Gitee documents target_commitish as the branch/commit of a newly created release tag.
    body = {
        "access_token": token, "tag_name": candidate.gitee_tag, "target_commitish": "master",
        "name": candidate.gitee_tag, "body": f"Official upstream JAR: {candidate.url}", "prerelease": False,
    }
    return json_request(GITEE_API + "/releases", hosts={"gitee.com"},
                        data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})


def gitee_upload(release_id, candidate, data, token):
    boundary = secrets.token_hex(24)
    meta = f'--{boundary}\r\nContent-Disposition: form-data; name="access_token"\r\n\r\n{token}\r\n'
    part = f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{candidate.file}"\r\nContent-Type: application/java-archive\r\n\r\n'
    body = (meta + part).encode() + data + f"\r\n--{boundary}--\r\n".encode()
    json_request(f"{GITEE_API}/releases/{release_id}/attach_files", hosts={"gitee.com"}, data=body,
                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}, timeout=90)


def find_release(candidate, token):
    try:
        return gitee_read("/releases/tags/" + candidate.gitee_tag, token)
    except HttpStatus as exc:
        if exc.code != 404:
            raise
        return None


def gitee_download(candidate, expected):
    for i in range(4):
        try:
            mirrored = request(candidate.gitee_url, hosts=GITEE_HOSTS, limit=MAX_JAR, redirects=True)
            if len(mirrored) != candidate.size or sha(mirrored) != expected:
                raise SyncError("Gitee attachment SHA-256 mismatch; nothing overwritten")
            return
        except HttpStatus as exc:
            if exc.code not in (404, 429, 500, 502, 503, 504) or i == 3:
                raise
            time.sleep(2 ** i)


def gitee_stage(stage, action, *args):
    try:
        return action(*args)
    except SyncError as exc:
        raise SyncError(f"Gitee {stage}: {exc}") from None


def list_attachments(release_id, token):
    attachments = gitee_read(f"/releases/{release_id}/attach_files", token)
    if not isinstance(attachments, list):
        raise SyncError("Unexpected Gitee attachments response")
    return attachments


def probe_existing_release(candidate, token):
    release = gitee_stage("release lookup", find_release, candidate, token)
    if release is None:
        print(f"GITEE READ CHECK {candidate.gitee_tag}: no existing release")
        return
    release_id = release.get("id")
    if type(release_id) is not int or release_id <= 0:
        raise SyncError("Unexpected Gitee release identity")
    attachments = gitee_stage("attachment listing", list_attachments, release_id, token)
    print(f"GITEE READ CHECK {candidate.gitee_tag}: {len(attachments)} attachments; no writes", flush=True)
    if candidate.digest and sum(item.get("name") == candidate.file for item in attachments) == 1:
        gitee_stage("anonymous attachment verification", gitee_download, candidate, candidate.digest.removeprefix("sha256:"))
        print(f"GITEE PUBLIC DOWNLOAD VERIFIED {candidate.file}; no writes", flush=True)
        published = read_public_manifest("latest.json")
        row = next(row for row in published["mods"] if row["id"] == candidate.mod)
        if (row.get("version"), row.get("sha256"), row.get("url")) == (candidate.version, candidate.digest.removeprefix("sha256:"), candidate.gitee_url):
            print(f"GITEE PUBLIC MANIFEST VERIFIED {candidate.file}; no writes", flush=True)
        else:
            print(f"GITEE PUBLIC MANIFEST {candidate.mod} currently lists {row.get('version')}; no writes", flush=True)


def copy(candidate, token, apply):
    try:
        data = request(candidate.url, hosts=GH_HOSTS, limit=MAX_JAR, redirects=True)
    except HttpStatus as exc:
        raise SyncError(f"GitHub JAR download returned HTTP {exc.code}") from None
    inspect_jar(candidate, data)
    digest = sha(data)
    if not apply:
        print(f"DRY RUN {candidate.gitee_tag}: {candidate.file} SHA256={digest}; no Gitee writes.")
        return
    print(f"UPSTREAM VERIFIED {candidate.file} SHA256={digest}", flush=True)
    existing = gitee_stage("release lookup", find_release, candidate, token)
    release = existing if existing is not None else gitee_stage("release creation", gitee_create, candidate, token)
    if release.get("tag_name") != candidate.gitee_tag or type(release.get("id")) is not int or release["id"] <= 0:
        raise SyncError("Unexpected Gitee release identity")
    release_id = release["id"]
    attachments = gitee_stage("attachment listing", list_attachments, release_id, token)
    matches = [item for item in attachments if item.get("name") == candidate.file]
    if len(matches) > 1:
        raise SyncError("Ambiguous Gitee attachments; no overwrite")
    if not matches:
        print(f"GITEE ATTACHMENT UPLOAD {candidate.file}", flush=True)
        try:
            gitee_stage("attachment upload", gitee_upload, release_id, candidate, data, token)
        except SyncError as upload_error:
            # A lost response may follow a successful upload. Recheck, never replay the POST here.
            try:
                recovered = gitee_stage("attachment listing after upload", list_attachments, release_id, token)
            except SyncError:
                raise upload_error from None
            if len([item for item in recovered if item.get("name") == candidate.file]) != 1:
                raise upload_error from None
    gitee_stage("anonymous attachment verification", gitee_download, candidate, digest)
    print(f"{'VERIFIED' if matches else 'SYNCED'} {candidate.gitee_tag}: {candidate.gitee_url} SHA256={digest}")
    return {"id": candidate.mod, "version": candidate.version, "file": candidate.file,
            "bytes": candidate.size, "sha256": digest, "url": candidate.gitee_url}


def json_object(pairs):
    result = {}
    for key, val in pairs:
        if key in result:
            raise SyncError(f"Duplicate JSON key: {key}")
        result[key] = val
    return result


def safely_read_manifest(raw, kind):
    try:
        doc = json.loads(raw, object_pairs_hook=json_object)
    except (TypeError, ValueError):
        raise SyncError(f"Invalid {kind} manifest") from None
    expected = {"schema": 1, "packId": "ncepu-hd", "minecraft": "1.20.1", "kind": kind}
    if not isinstance(doc, dict) or any(doc.get(k) != v for k, v in expected.items()):
        raise SyncError(f"Unknown {kind} schema, pack or game version")
    rows = doc.get("mods")
    if not isinstance(rows, list) or any(not isinstance(r, dict) or not isinstance(r.get("id"), str) for r in rows):
        raise SyncError(f"Unknown {kind} mods representation")
    ids = [row["id"] for row in rows]
    if len(set(ids)) != len(ids) or set(MODS) - set(ids):
        raise SyncError(f"Missing or duplicate {kind} mod IDs")
    if kind == "latest" and set(ids) != set(MODS):
        raise SyncError("Unexpected latest.json mod IDs; no change")
    return doc


def manifest_fingerprint(rows):
    return sha(json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def version_number(text):
    return tuple(map(int, version_of(text).split(".")))


def patch_manifest(doc, replacements, scheme=None):
    out = deepcopy(doc)
    for i, old in enumerate(out["mods"]):
        new = replacements.get(old["id"])
        if new is None:
            continue
        prior, target = version_number(old["version"]), version_number(new["version"])
        if target < prior:
            print(f"SKIP {old['id']}: manifest already newer ({old['version']}); no automatic downgrade.")
            continue
        if target == prior:
            if old.get("sha256") != new["sha256"] or old.get("bytes") != new["bytes"]:
                raise SyncError("Same-version asset bytes changed; manifests unchanged")
            continue
        out["mods"][i] = {**old, **new}
    if out != doc and doc["kind"] == "install" and "assetSet" in doc:
        if scheme == "canonical-json-sha256" and doc["assetSet"] == manifest_fingerprint(doc["mods"]):
            out["assetSet"] = manifest_fingerprint(out["mods"])
        elif scheme != "preserve":
            raise SyncError("Cannot verify existing install.json assetSet; use latest-only until confirmed")
    return out


def git(directory, args, extra_env=None):
    # Override ambient hooks/config and return only contextual diagnostics, never raw token-bearing output.
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0"}
    for key in list(env):
        if key.startswith(("GIT_TRACE", "GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")) or key in (
                "GH_TOKEN", "GITEE_TOKEN", "GITEE_GIT_PASSWORD", "GIT_ASKPASS", "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS", "GIT_CURL_VERBOSE"):
            env.pop(key)
    env.update(extra_env or {})
    try:
        p = subprocess.run(["git", "-c", f"core.hooksPath={os.devnull}", "-c", "http.followRedirects=false", *args],
                           cwd=directory, env=env, check=False, capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        raise SyncError("Gitee Git command unavailable or timed out") from None
    if p.returncode:
        raise SyncError("Git " + args[0] + " failed; check credentials, branch protection or a concurrent update")
    return p.stdout


def read_public_manifest(name):
    if name not in {"latest.json", "install.json"}:
        raise SyncError("Unapproved raw manifest")
    url = f"{GITEE_WEB}/raw/master/{name}"
    raw = request(url, hosts=GITEE_RAW_HOSTS, headers={"Cache-Control": "no-cache"}, redirects=True)
    return safely_read_manifest(raw, name.removesuffix(".json"))


def confirm_public_manifests(expected):
    """Check the same anonymous endpoints the launcher reads; never retry a push."""
    for attempt in range(3):
        try:
            for name, document in expected.items():
                if read_public_manifest(name) != document:
                    raise SyncError("Public manifest has not caught up to the push")
            return
        except SyncError as exc:
            if attempt == 2:
                raise SyncError(f"Anonymous raw contents were not confirmed: {exc}; do not re-push blindly") from None
            time.sleep(2 ** attempt)

def publish_manifests(replacements, mode):
    if not replacements or mode == "none":
        return
    scheme = os.environ.get("GITEE_ASSETSET_SCHEME", "")
    if mode == "paired" and scheme not in {"canonical-json-sha256", "preserve"}:
        raise SyncError("Paired mode needs a verified GITEE_ASSETSET_SCHEME; latest-only is safe meanwhile")
    paths = ["latest.json"] if mode == "latest" else ["latest.json", "install.json"]
    with tempfile.TemporaryDirectory(prefix="mc-gitee-manifests-") as temp:
        folder = Path(temp)
        git(folder, ["clone", "--no-checkout", "--no-tags", "--depth", "1", "--single-branch",
                     "--branch", "master", GITEE_WEB + ".git", "repo"])
        repo = folder / "repo"
        git(repo, ["read-tree", "HEAD"])
        originals, finals = {}, {}
        for name in paths:
            kind = name.removesuffix(".json")
            originals[name] = safely_read_manifest(git(repo, ["show", f"HEAD:{name}"]), kind)
            finals[name] = patch_manifest(originals[name], replacements, scheme)
        if mode == "paired":
            managed = set(replacements)
            rows_by_manifest = [{r["id"]: r for r in doc["mods"] if r["id"] in managed} for doc in finals.values()]
            keys = ("id", "version", "file", "bytes", "sha256", "url")
            if any(any(rows_by_manifest[0][id].get(k) != rows_by_manifest[1][id].get(k) for k in keys) for id in managed):
                raise SyncError("Paired manifests would disagree on a managed row; nothing changed")
        changed = [name for name in paths if originals[name] != finals[name]]
        if not changed:
            confirm_public_manifests(finals)
            print("No manifest change needed; current public manifest verified.")
            return
        for name in changed:
            (repo / name).write_text(json.dumps(finals[name], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        git(repo, ["add", "--", *changed])
        staged = git(repo, ["diff", "--cached", "--name-only", "-z"]).decode().strip("\0").split("\0")
        if set(staged) != set(changed):
            raise SyncError("Unexpected staged files; no commit or push")
        git(repo, ["-c", "user.name=MC Client Pack Sync", "-c", "user.email=mc-client-pack-sync@users.noreply.gitee.com",
                   "commit", "-m", "Update verified client mod release metadata"])
        askpass = folder / "askpass"
        askpass.write_text('#!/bin/sh\ncase "$1" in *Username*) printf "%s\\n" "$MC_GIT_USER";; *Password*) printf "%s\\n" "$MC_GIT_TOKEN";; *) exit 1;; esac\n')
        askpass.chmod(0o700)
        user = os.environ.get("GITEE_GIT_USER", "intelsayamdyes")
        pwd = os.environ.get("GITEE_GIT_PASSWORD") or os.environ["GITEE_TOKEN"]
        git(repo, ["push", "origin", "HEAD:refs/heads/master"],
            {"GIT_ASKPASS": str(askpass), "MC_GIT_USER": user, "MC_GIT_TOKEN": pwd, "LC_ALL": "C"})
        published = git(repo, ["rev-parse", "HEAD"]).decode().strip()
        confirm_public_manifests({name: finals[name] for name in changed})
        print(f"Published and anonymously verified only {', '.join(changed)} in Gitee master commit {published}.")

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Create Gitee releases/assets; default dry run.")
    parser.add_argument("--manifest-mode", choices=("none", "latest", "paired"), default="none",
                        help="On apply, publish verified manifest changes (none, latest only, or both).")
    parser.add_argument("--probe-gitee", action="store_true", help="With token, check Gitee release/attachment listing without writes.")
    parser.add_argument("--mod", choices=tuple(MODS), help="Limit to one approved mod.")
    parser.add_argument("--tag", help="Use a specific stable GitHub tag, e.g. v1.0.19; requires --mod.")
    args = parser.parse_args()
    if args.tag and (not args.mod or not VERSION.fullmatch(args.tag)):
        parser.error("--tag requires --mod and a stable x.y.z GitHub tag")
    token = os.environ.get("GITEE_TOKEN", "")
    if args.apply and not re.fullmatch(r"[A-Za-z0-9_.~+/-]{8,512}", token):
        parser.error("Set GITEE_TOKEN via GitHub Actions Secrets before applying")
    failed = False
    replacements = {}
    for mod in (args.mod,) if args.mod else MODS:
        try:
            candidate = get_github(mod, args.tag)
            if args.probe_gitee and not args.apply and mod == "bankmod":
                if token:
                    probe_existing_release(candidate, token)
                else:
                    print("GITEE READ CHECK skipped: GITEE_TOKEN is not configured")
            record = copy(candidate, token, args.apply)
            if record:
                replacements[mod] = record
        except (SyncError, TypeError, AttributeError, KeyError, ValueError) as exc:
            failed = True
            # No raw tracebacks/bodies/URLs: an HTTP error can include a sensitive query.
            detail = str(exc) if isinstance(exc, SyncError) else "Unexpected response structure"
            print(f"{mod}: {detail}", file=sys.stderr)
    if not failed and args.apply:
        try:
            publish_manifests(replacements, args.manifest_mode)
        except (SyncError, TypeError, AttributeError, KeyError, ValueError) as exc:
            detail = str(exc) if isinstance(exc, SyncError) else "Unexpected manifest structure"
            print(f"Manifest sync incomplete: {detail}", file=sys.stderr)
            failed = True
    return int(failed)


if __name__ == "__main__":
    sys.exit(main())
