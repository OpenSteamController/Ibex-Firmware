#!/usr/bin/env python3
"""Maintain the Ibex firmware archive.

Subcommands:
    backfill <SteamTracking-clone-path>
        Walk full git history of the four manifests, populate / refresh
        index.json, extract any .fw files not yet on disk. Run once.

    check
        Walk every SteamTracking commit since the recorded last_check on
        each live channel (stable, publicbeta) via the GitHub REST API,
        download any new bins_hardware_all zip, extract new .fw files,
        update index.json, advance last_check. Prints `changed=true|false`
        to $GITHUB_OUTPUT.

    last-commit-message
        Print a one-line summary of the latest update to index.json,
        suitable for a git commit subject.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INDEX_PATH = REPO_ROOT / "index.json"
CONTROLLER_DIR = REPO_ROOT / "Controller"
PUCK_DIR = REPO_ROOT / "Puck"

CDN = "https://cdn.steamstatic.com/client/"
UA = "ibex-firmware-tracker/1.0 (+https://github.com/MasterR3C0RD/IbexFirmware)"

# (logical channel, manifest path inside SteamTracking)
LIVE_MANIFESTS = [
    ("stable",     "ClientManifest/steam_client_win64"),
    ("publicbeta", "ClientManifest/steam_client_publicbeta_win64"),
]
# Used only by backfill — these manifest paths existed historically but the win32
# ones have been retired from SteamTracking. Walking them gives us early history.
BACKFILL_MANIFESTS = [
    ("stable",     "ClientManifest/steam_client_win32"),
    ("stable",     "ClientManifest/steam_client_win64"),
    ("publicbeta", "ClientManifest/steam_client_publicbeta_win32"),
    ("publicbeta", "ClientManifest/steam_client_publicbeta_win64"),
]

FILE_RE = re.compile(
    r'"bins_hardware_all"\s*\{[^}]*?"file"\s*"([^"]+)"',
    re.DOTALL,
)
VERSION_RE = re.compile(r'"version"\s*"(\d+)"')


# --- header / metadata --------------------------------------------------------

def parse_firmware(data: bytes) -> dict:
    """Parse an Ibex .fw blob. Verifies the stored CRC matches.

    Returns a dict suitable for storing under controller[fname] / puck[fname]
    in index.json (minus first_seen / source_zips, which the caller fills in).
    """
    if len(data) < 0x20:
        raise ValueError(f"firmware too short: {len(data)} bytes")
    payload_size = struct.unpack("<I", data[0x04:0x08])[0]
    stored_crc = struct.unpack("<I", data[0x08:0x0C])[0]
    if len(data) != 0x20 + payload_size:
        raise ValueError(
            f"size mismatch: header says payload={payload_size}, "
            f"file is {len(data)} (expected {0x20 + payload_size})"
        )
    computed = zlib.crc32(data[0x20:0x20 + payload_size]) & 0xFFFFFFFF
    if computed != stored_crc:
        raise ValueError(
            f"CRC mismatch: header={stored_crc:#010x}, computed={computed:#010x}"
        )
    return {
        "file_size": len(data),
        "payload_size": payload_size,
        "crc32": f"{stored_crc:08x}",
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def version_from_filename(fname: str) -> dict:
    """`IBEX_FW_69FE17FF.fw` -> {version_hex, version_unix}. Returns empty dict
    if the filename doesn't match the expected stem."""
    m = re.match(r"^(?:IBEX_FW|PROTEUS_FW)_([0-9A-Fa-f]{8})\.fw(?:\.\d+)?$", fname)
    if not m:
        return {}
    h = m.group(1).upper()
    return {"version_hex": h, "version_unix": int(h, 16)}


def category_for(fname: str) -> str | None:
    if fname.startswith("IBEX_FW_"):
        return "controller"
    if fname.startswith("PROTEUS_FW_"):
        return "puck"
    return None


def dir_for(category: str) -> Path:
    return CONTROLLER_DIR if category == "controller" else PUCK_DIR


# --- index.json ---------------------------------------------------------------

def empty_index() -> dict:
    return {
        "generated_at": "",
        "controller": {},
        "puck": {},
        "crc32_index": {},
        "_zips_seen": {},
        "_channels": {},
    }


def load_index() -> dict:
    if INDEX_PATH.exists():
        return json.loads(INDEX_PATH.read_text())
    return empty_index()


def build_crc32_index(index: dict) -> dict[str, dict]:
    """Reverse-lookup: crc32 hex -> {path, category, version_hex}. The Steam
    Controller in bootloader mode only surfaces its firmware's payload CRC, so
    this is what clients use to resolve "running CRC -> which version is it".
    """
    out: dict[str, dict] = {}
    for cat, subdir in (("controller", "Controller"), ("puck", "Puck")):
        for fname, rec in index.get(cat, {}).items():
            crc = rec.get("crc32")
            if not crc:
                continue
            entry = {
                "path": f"{subdir}/{fname}",
                "category": cat,
                "version_hex": rec.get("version_hex", ""),
            }
            if crc in out and out[crc] != entry:
                # Two firmware files with the same payload CRC. Realistically
                # impossible by collision (32 bits, ~30 files), but Valve could
                # ship the same payload under a new build tag. Surface loudly.
                raise RuntimeError(
                    f"CRC32 collision on {crc}: {out[crc]} vs {entry}"
                )
            out[crc] = entry
    return out


def save_index(index: dict) -> None:
    index["generated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    index["crc32_index"] = build_crc32_index(index)
    INDEX_PATH.write_text(json.dumps(index, indent=2, sort_keys=True) + "\n")


def iso_from_unix(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- HTTP / GitHub API --------------------------------------------------------

def http_get(url: str, headers: dict | None = None, retries: int = 3) -> bytes:
    h = {"User-Agent": UA}
    if headers:
        h.update(headers)
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            # Don't retry 4xx unless it's a rate-limit
            if 400 <= e.code < 500 and e.code not in (403, 408, 429):
                raise
            last_err = e
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
        time.sleep(2 ** attempt)
    assert last_err is not None
    raise last_err


def gh_headers() -> dict:
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    tok = os.environ.get("GITHUB_TOKEN")
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


def gh_commits_since(path: str, stop_sha: str | None) -> list[dict]:
    """List commits on `path` newer than `stop_sha`, returned oldest-first.

    If stop_sha is None, returns *all* commits on the path (you almost never
    want this in `check`; backfill uses the local git history instead). The
    GitHub commits API returns newest-first paginated by 100; we walk pages
    until we hit stop_sha or run out.
    """
    out: list[dict] = []
    page = 1
    while True:
        qs = urllib.parse.urlencode({
            "path": path,
            "sha": "master",
            "per_page": "100",
            "page": str(page),
        })
        url = f"https://api.github.com/repos/SteamDatabase/SteamTracking/commits?{qs}"
        body = http_get(url, headers=gh_headers())
        batch = json.loads(body)
        if not batch:
            break
        hit_stop = False
        for c in batch:
            if stop_sha and c["sha"] == stop_sha:
                hit_stop = True
                break
            out.append(c)
        if hit_stop or len(batch) < 100:
            break
        page += 1
    out.reverse()  # oldest first
    return out


def raw_manifest(sha: str, path: str) -> str:
    url = f"https://raw.githubusercontent.com/SteamDatabase/SteamTracking/{sha}/{path}"
    return http_get(url).decode("utf-8", errors="replace")


# --- zip download / extraction ------------------------------------------------

def cached_download(filename: str, cache_dir: Path) -> bytes:
    cache_dir.mkdir(parents=True, exist_ok=True)
    p = cache_dir / filename
    if p.exists():
        return p.read_bytes()
    print(f"    downloading {filename}", flush=True)
    data = http_get(CDN + filename)
    p.write_bytes(data)
    return data


def parse_manifest_text(text: str) -> tuple[str | None, str | None]:
    """Return (zip_filename, steam_version) or (None, None) if the block isn't
    present."""
    fm = FILE_RE.search(text)
    vm = VERSION_RE.search(text)
    return (
        fm.group(1) if fm else None,
        vm.group(1) if vm else None,
    )


def extract_zip(
    zip_filename: str,
    zip_bytes: bytes,
    index: dict,
) -> tuple[list[str], bool]:
    """Open zip, extract any .fw files we don't already have. Returns
    (member_filenames, anything_new). Members are appended to
    _zips_seen[zip].members."""
    zip_entry = index["_zips_seen"].setdefault(
        zip_filename, {"first_seen": {}, "members": []}
    )
    members_recorded: set[str] = set(zip_entry["members"])
    extracted_new = False

    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        print(f"    !! not a valid zip: {zip_filename}", flush=True)
        return [], False

    fw_members = [n for n in zf.namelist() if n.lower().endswith(".fw")]
    for member in fw_members:
        basename = Path(member).name
        cat = category_for(basename)
        if cat is None:
            print(f"    !! unrecognized firmware name: {basename}", flush=True)
            continue
        target = dir_for(cat) / basename
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(member) as src:
            blob = src.read()
        try:
            meta = parse_firmware(blob)
        except ValueError as e:
            print(f"    !! header check failed for {basename}: {e}", flush=True)
            continue

        if not target.exists():
            target.write_bytes(blob)
            extracted_new = True
            print(f"    + {cat}/{basename}", flush=True)

        record = index[cat].setdefault(basename, {})
        record.update(meta)
        record.update(version_from_filename(basename))
        record.setdefault("first_seen", {})
        zips_for_fw = set(record.get("source_zips", []))
        zips_for_fw.add(zip_filename)
        record["source_zips"] = sorted(zips_for_fw)

        if basename not in members_recorded:
            zip_entry["members"].append(basename)
            members_recorded.add(basename)

    zip_entry["members"] = sorted(set(zip_entry["members"]))
    return zip_entry["members"], extracted_new


def record_first_seen(
    index: dict,
    zip_filename: str,
    channel: str,
    *,
    steam_version: str | None,
    date_iso: str,
    commit: str | None,
) -> bool:
    """Set first_seen[channel] on the zip and on every member firmware,
    only if not already set. Returns True if anything was newly recorded."""
    changed = False
    fs = {"steam_version": steam_version, "date": date_iso}
    if commit:
        fs["commit"] = commit

    zip_entry = index["_zips_seen"].setdefault(
        zip_filename, {"first_seen": {}, "members": []}
    )
    if channel not in zip_entry["first_seen"]:
        zip_entry["first_seen"][channel] = fs
        changed = True

    for member in zip_entry["members"]:
        cat = category_for(member)
        if cat is None:
            continue
        rec = index[cat].setdefault(member, {})
        rec_fs = rec.setdefault("first_seen", {})
        if channel not in rec_fs:
            rec_fs[channel] = fs
            changed = True
    return changed


# --- backfill -----------------------------------------------------------------

def git_in(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], text=True, errors="replace"
    )


def backfill(steamtracking_path: str) -> None:
    repo = Path(steamtracking_path).resolve()
    if not (repo / ".git").exists():
        sys.exit(f"not a git repo: {repo}")

    cache_dir = REPO_ROOT / ".zip_cache"
    index = load_index()
    CONTROLLER_DIR.mkdir(exist_ok=True)
    PUCK_DIR.mkdir(exist_ok=True)

    # Per-channel: zip_filename -> (earliest_unix_ts, sha, steam_version)
    earliest: dict[str, dict[str, tuple[int, str, str | None]]] = {
        "stable": {}, "publicbeta": {}
    }
    # Per-channel HEAD-relative latest commit on master that touched any of
    # the channel's manifests — used to seed _channels[ch].last_check_commit.
    channel_latest_master_sha: dict[str, tuple[int, str]] = {}
    master_sha = git_in(repo, "rev-parse", "master").strip()

    for channel, manifest in BACKFILL_MANIFESTS:
        try:
            log = git_in(
                repo, "log", "--format=%ct %H", "master", "--", manifest
            )
        except subprocess.CalledProcessError:
            print(f"  (no history for {manifest})", flush=True)
            continue
        rows = []
        for line in log.splitlines():
            if not line:
                continue
            ts, sha = line.split(None, 1)
            rows.append((int(ts), sha))
        print(f"{channel:11s} {manifest}: {len(rows)} commits", flush=True)
        # Oldest first for "earliest" semantics.
        for ts, sha in sorted(rows, key=lambda r: r[0]):
            try:
                text = git_in(repo, "show", f"{sha}:{manifest}")
            except subprocess.CalledProcessError:
                continue
            zname, sver = parse_manifest_text(text)
            if not zname:
                continue
            prev = earliest[channel].get(zname)
            if prev is None or ts < prev[0]:
                earliest[channel][zname] = (ts, sha, sver)
            cur_latest = channel_latest_master_sha.get(channel)
            if cur_latest is None or ts > cur_latest[0]:
                channel_latest_master_sha[channel] = (ts, sha)

    # Build the set of all unique zips we need to download.
    all_zips: set[str] = set()
    for ch in earliest.values():
        all_zips.update(ch.keys())
    print(f"\n{len(all_zips)} unique zip(s) across all channels", flush=True)

    for zname in sorted(all_zips):
        try:
            blob = cached_download(zname, cache_dir)
        except Exception as e:
            print(f"  ! failed to download {zname}: {e}", flush=True)
            continue
        extract_zip(zname, blob, index)

    # Record first_seen on each channel.
    for channel, zmap in earliest.items():
        for zname, (ts, sha, sver) in zmap.items():
            record_first_seen(
                index, zname, channel,
                steam_version=sver,
                date_iso=iso_from_unix(ts),
                commit=sha,
            )

    # Seed _channels[ch].last_check_commit. For each live channel use the most
    # recent SteamTracking master sha we observed touching its current manifest.
    # If we never saw one (rare — channel manifest exists but no commits in our
    # local clone), fall back to HEAD.
    for channel, manifest in LIVE_MANIFESTS:
        head_for_path = git_in(
            repo, "log", "-1", "--format=%H", "master", "--", manifest
        ).strip()
        sha = head_for_path or master_sha
        index["_channels"][channel] = {
            "last_check_commit": sha,
            "last_check_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

    save_index(index)
    print(f"\nbackfill complete. index.json written.", flush=True)


# --- check --------------------------------------------------------------------

def check() -> None:
    index = load_index()
    CONTROLLER_DIR.mkdir(exist_ok=True)
    PUCK_DIR.mkdir(exist_ok=True)
    cache_dir = REPO_ROOT / ".zip_cache"

    any_change = False
    new_firmware_files: list[str] = []

    for channel, manifest in LIVE_MANIFESTS:
        ch_state = index["_channels"].get(channel) or {}
        stop_sha = ch_state.get("last_check_commit")
        print(f"[{channel}] stop_sha={stop_sha or '(none)'}", flush=True)

        commits = gh_commits_since(manifest, stop_sha)
        print(f"  {len(commits)} new commit(s) on {manifest}", flush=True)

        # Track zips we've already handled this channel-run to avoid double-work
        seen_this_run: set[str] = set()

        for c in commits:
            sha = c["sha"]
            date_iso = c["commit"]["committer"]["date"]
            try:
                text = raw_manifest(sha, manifest)
            except urllib.error.HTTPError as e:
                # Path may not exist at that revision (manifest was added later
                # or removed). Skip — we still advance past it.
                print(f"    skip {sha[:9]}: HTTP {e.code} fetching manifest", flush=True)
                continue
            zname, sver = parse_manifest_text(text)
            if not zname:
                continue
            if zname in seen_this_run:
                # Same zip seen earlier in this same channel's walk — first_seen
                # already pinned to the earlier commit. Skip.
                continue
            seen_this_run.add(zname)

            zip_entry = index["_zips_seen"].get(zname)
            if zip_entry is None:
                # Brand new zip.
                try:
                    blob = cached_download(zname, cache_dir)
                except Exception as e:
                    print(f"    ! failed to download {zname}: {e}", flush=True)
                    continue
                members, anything_new = extract_zip(zname, blob, index)
                if anything_new:
                    any_change = True
                    new_firmware_files.extend(members)
                # Record first_seen on this channel using this commit.
                record_first_seen(
                    index, zname, channel,
                    steam_version=sver, date_iso=date_iso, commit=sha,
                )
                any_change = True
            else:
                # Known zip. Record first_seen on this channel if missing.
                if channel not in zip_entry["first_seen"]:
                    record_first_seen(
                        index, zname, channel,
                        steam_version=sver, date_iso=date_iso, commit=sha,
                    )
                    any_change = True

        # Advance the channel pointer to the newest commit we just saw, if any.
        # We don't bump last_check_at on no-op runs so re-running check locally
        # doesn't dirty the working tree.
        if commits:
            newest = commits[-1]["sha"]
            index["_channels"][channel] = {
                "last_check_commit": newest,
                "last_check_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }

    if any_change:
        save_index(index)

    gho = os.environ.get("GITHUB_OUTPUT")
    val = "true" if any_change else "false"
    print(f"changed={val}", flush=True)
    if gho:
        with open(gho, "a") as f:
            f.write(f"changed={val}\n")
            if new_firmware_files:
                f.write("new_firmware=" + ",".join(sorted(set(new_firmware_files))) + "\n")


# --- last-commit-message ------------------------------------------------------

def last_commit_message() -> None:
    """Inspect index.json and print a short commit subject for the workflow.

    Picks the most recent first_seen entry across all firmware records and
    reports the firmware(s) discovered at that timestamp + channel.
    """
    index = load_index()
    latest_ts = ""
    latest_channel = ""
    latest_names: list[tuple[str, str]] = []
    for cat in ("controller", "puck"):
        for name, rec in index.get(cat, {}).items():
            for ch, fs in rec.get("first_seen", {}).items():
                d = fs.get("date", "")
                if d > latest_ts:
                    latest_ts = d
                    latest_channel = ch
                    latest_names = [(cat, name)]
                elif d == latest_ts and d:
                    latest_names.append((cat, name))
    if not latest_names:
        print("firmware index updated")
        return
    pretty = ", ".join(f"{n} ({c})" for c, n in sorted(latest_names))
    print(f"add {pretty} from {latest_channel}")


# --- entrypoint ---------------------------------------------------------------

def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    cmd = sys.argv[1]
    if cmd == "backfill":
        if len(sys.argv) != 3:
            sys.exit("usage: ibex_firmware.py backfill <SteamTracking-clone>")
        backfill(sys.argv[2])
    elif cmd == "check":
        check()
    elif cmd == "last-commit-message":
        last_commit_message()
    else:
        sys.exit(f"unknown subcommand: {cmd}")


if __name__ == "__main__":
    main()
