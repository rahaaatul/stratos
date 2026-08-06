#!/usr/bin/env python3
"""
fetch_module_builds.py — Stratos unified module build fetcher

Rules:
- Priority: Release(0) > Pre-release(1) > Action(2) for checksum/filename dedup
- Naming: MMRL format {clean_version}_{versionCode}.zip
- Retention: per-source keep: is absolute cap, never exceeded
- preserve: true on release guarantees >= 1 stable always retained
- Fetch optimization: fetch only what's needed (newest to oldest), stop at keep count
- Same-source filename collisions: error out with clear logging
- Checksum verification every run (bandwidth not a concern)
- update.json rebuilt from scratch every run (no ghosts possible)
- track.yaml rebuilt from scratch every run (full rewrite, no partial updates)
- Zips never modified, only read for module.prop
- Raw URLs everywhere
- Pattern matching: supports both regex and glob. No pattern = first .zip wins.
"""

import fnmatch
import hashlib
import json
import os
import re
import shutil
import sys
import time
import zipfile
from pathlib import Path

import requests
import yaml

REPO_RAW = "https://raw.githubusercontent.com/rahaaatul/stratos/refs/heads/master"
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MODULES_DIR = REPO_ROOT / "modules"
CONFIG_FILE = REPO_ROOT / "modules.yaml"

GITHUB_TOKEN = os.environ.get("GH_TOKEN", "")
ARTIFACT_PAT = os.environ.get("ARTIFACT_PAT", "")

# Priority: Release > Pre-release > Action
PRIORITY = {"release": 0, "pre-release": 1, "action": 2}


def get_repo_metadata(owner, repo, token):
    """Fetch repo metadata: license, default_branch, owner avatar."""
    if not owner or not repo:
        return {}
    url = f"https://api.github.com/repos/{owner}/{repo}"
    headers = gh_headers(token)
    try:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        return {
            "license": data.get("license", {}).get("spdx_id", "") or "",
            "default_branch": data.get("default_branch", "main"),
            "owner_avatar": data.get("owner", {}).get("avatar_url", ""),
        }
    except Exception as e:
        print(f"  [warn] repo metadata fetch failed: {e}")
        return {"license": "", "default_branch": "main", "owner_avatar": ""}


def write_track_yaml(track_file, mod_cfg, metadata, last_update_ts):
    """Generate complete track.yaml from modules.yaml + upstream metadata.
    Always overwrites from scratch — never appends. Called every run.

    Fields:
    - id, enable, verified, categories: from modules.yaml
    - license: modules.yaml override, else upstream
    - icon: modules.yaml override, else upstream owner avatar
    - readme: auto-derived from upstream (never hand-set)
    - source: hardcoded to Stratos repo
    - update_to: hardcoded to our update.json
    - added: when WE stored the zip (current time)
    - last_update: upstream's release timestamp (newest across retained)
    """
    mod_id = mod_cfg["id"]
    owner = mod_cfg.get("owner", "")
    repo = mod_cfg.get("repo", "")

    license_val = mod_cfg.get("license", "") or metadata.get("license", "")
    icon_val = mod_cfg.get("icon", "") or metadata.get("owner_avatar", "")
    default_branch = metadata.get("default_branch", "main")
    readme_url = (
        f"https://raw.githubusercontent.com/{owner}/{repo}/{default_branch}/README.md"
        if owner and repo else ""
    )

    lines = [
        f"id: {mod_id}",
        f"enable: {str(mod_cfg.get('enabled', True)).lower()}",
        f"verified: {str(mod_cfg.get('verified', False)).lower()}",
    ]

    if mod_cfg.get("categories"):
        lines.append("categories:")
        for cat in mod_cfg["categories"]:
            lines.append(f"  - {cat}")

    if license_val:
        lines.append(f"license: {license_val}")
    if icon_val:
        lines.append(f"icon: {icon_val}")
    if readme_url:
        lines.append(f"readme: {readme_url}")

    lines.extend([
        "source: https://github.com/rahaaatul/stratos",
        f"update_to: {REPO_RAW}/modules/{mod_id}/update.json",
        f"added: {int(time.time())}",
    ])

    if last_update_ts > 0:
        lines.append(f"last_update: {last_update_ts}")

    track_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


def matches_pattern(pattern, name):
    """Match a filename against a pattern.

    Supports both regex and glob:
      - Regex: r"-release\\.zip$", r"v\\d+-Integrity-Box-\\d{2}-\\d{2}-\\d{4}\\.zip"
      - Glob: "*.zip", "*-release.zip", "Frosty-*.zip"

    Resolution order:
      1. Empty/None pattern -> match everything
      2. Try regex (re.search, case-insensitive)
      3. If regex fails to compile, fall back to glob (fnmatch, case-insensitive)
      4. If regex compiles but doesn't match, also try glob as secondary fallback
    """
    if not pattern:
        return True

    try:
        if re.search(pattern, name, re.IGNORECASE):
            return True
    except re.error:
        pass

    if fnmatch.fnmatch(name.lower(), pattern.lower()):
        return True

    return False


def clean_version(raw):
    """Strip parenthetical/build junk. 'v2.2 (3080-88f8e1fa-JingMatrix-Vector)' -> 'v2.2'"""
    if not raw:
        return "unknown"
    return re.split(r"[\s(]", raw, maxsplit=1)[0].strip() or "unknown"


def mmrl_zip_name(clean_ver, version_code):
    return f"{clean_ver}_{version_code}.zip"


def mmrl_md_name(clean_ver, version_code):
    return f"{clean_ver}_{version_code}.md"


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def read_module_prop(zip_path):
    """Read module.prop in memory. Never modifies the zip."""
    try:
        with zipfile.ZipFile(zip_path, "r") as z:
            for name in z.namelist():
                if name.endswith("module.prop"):
                    data = z.read(name).decode("utf-8", errors="replace")
                    props = {}
                    for line in data.splitlines():
                        if "=" in line and not line.startswith("#"):
                            k, _, v = line.partition("=")
                            props[k.strip()] = v.strip()
                    return props
    except Exception:
        pass
    return {}


def download_file(url, dest, headers):
    with requests.get(url, headers=headers, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)


def gh_headers(token):
    h = {"Accept": "application/vnd.github+json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def fetch_releases(owner, repo, pattern, want_pre, token, max_candidates):
    """Fetch release assets filtered by pattern. prerelease flag controls type.
    Fetches newest to oldest (GitHub API default), stops at max_candidates.
    If no pattern provided, defaults to first .zip found."""
    # Fetch slightly more than needed to allow for dedup buffer
    per_page = max(max_candidates * 3, 5)
    url = f"https://api.github.com/repos/{owner}/{repo}/releases?per_page={per_page}"
    headers = gh_headers(token)
    cands = []
    try:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        print(f"  [warn] release fetch failed: {e}")
        return []

    for rel in resp.json():
        if rel.get("draft"):
            continue
        if bool(rel.get("prerelease")) != want_pre:
            continue

        # Parse upstream release timestamp
        published_at = rel.get("published_at", "")
        upstream_ts = 0
        if published_at:
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
                upstream_ts = int(dt.timestamp())
            except Exception:
                pass

        for asset in rel.get("assets", []):
            name = asset["name"]
            if not matches_pattern(pattern, name):
                continue
            if not name.lower().endswith(".zip"):
                continue
            cands.append({
                "source": "pre-release" if want_pre else "release",
                "download_url": asset["browser_download_url"],
                "changelog": rel.get("body") or rel.get("name") or "",
                "use_headers": headers,
                "upstream_ts": upstream_ts,
            })
            # If no pattern, take only first .zip per release
            if not pattern:
                break

        # Stop once we have enough candidates
        if len(cands) >= max_candidates * 2:
            break

    return cands


def fetch_actions(owner, repo, workflow_file, pattern, token, max_candidates):
    """Fetch artifacts from successful runs of a specific workflow.
    Fetches newest to oldest, stops at max_candidates.
    If no pattern provided, defaults to first artifact found."""
    per_page = max(max_candidates * 3, 5)
    headers = gh_headers(token)
    url = (f"https://api.github.com/repos/{owner}/{repo}"
           f"/actions/workflows/{workflow_file}/runs?status=success&per_page={per_page}")
    cands = []
    try:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        runs = resp.json().get("workflow_runs", [])
        if not runs:
            print(f"  [warn] no successful runs for {workflow_file}")
            return []
    except Exception as e:
        print(f"  [warn] actions fetch failed: {e}")
        return []

    for run in runs:
        created_at = run.get("created_at", "")
        upstream_ts = 0
        if created_at:
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                upstream_ts = int(dt.timestamp())
            except Exception:
                pass

        arts_url = (f"https://api.github.com/repos/{owner}/{repo}"
                    f"/actions/runs/{run['id']}/artifacts")
        try:
            resp = requests.get(arts_url, headers=headers, timeout=30)
            resp.raise_for_status()
        except Exception:
            continue

        for art in resp.json().get("artifacts", []):
            name = art["name"]
            if not matches_pattern(pattern, name):
                continue
            if art.get("expired"):
                continue
            cands.append({
                "source": "action",
                "download_url": art["archive_download_url"],
                "changelog": run.get("display_title") or run.get("name") or "",
                "use_headers": headers,
                "upstream_ts": upstream_ts,
            })
            # If no pattern, take only first artifact per run
            if not pattern:
                break

        # Stop once we have enough candidates
        if len(cands) >= max_candidates * 2:
            break

    return cands


def fetch_static(url, changelog_url, headers):
    return [{
        "source": "release",
        "download_url": url,
        "changelog": changelog_url or "",
        "use_headers": headers,
        "is_static": True,
        "upstream_ts": 0,
    }]


def process_module(mod_cfg, gh_headers, pat_headers):
    mod_id = mod_cfg["id"]
    mod_dir = MODULES_DIR / mod_id
    mod_dir.mkdir(parents=True, exist_ok=True)
    print(f"\n=== {mod_id} ===")

    # Skip entirely if module-level enabled is false
    if not mod_cfg.get("enabled", True):
        print("  skipped (enabled: false in modules.yaml)")
        return

    rel_cfg = mod_cfg.get("release", {})
    pre_cfg = mod_cfg.get("prerelease", {})
    act_cfg = mod_cfg.get("actions", {})
    stat_cfg = mod_cfg.get("static", {})
    mod_owner = mod_cfg.get("owner", "")
    mod_repo = mod_cfg.get("repo", "")

    # Fetch upstream repo metadata for auto-derivation
    metadata = get_repo_metadata(mod_owner, mod_repo, GITHUB_TOKEN)

    candidates = []

    # 1. Static source
    if stat_cfg.get("enabled"):
        print(f"  checking static: {stat_cfg['url']}")
        candidates.extend(fetch_static(
            stat_cfg["url"], stat_cfg.get("changelog_url", ""), gh_headers
        ))

    # 2. Release source (prerelease=false ONLY)
    if rel_cfg.get("enabled"):
        owner = rel_cfg.get("owner", mod_owner)
        repo = rel_cfg.get("repo", mod_repo)
        pattern = rel_cfg.get("pattern", "")
        keep = rel_cfg.get("keep", 1)
        print(f"  checking release: {owner}/{repo} (keep={keep})")
        candidates.extend(fetch_releases(owner, repo, pattern, False, GITHUB_TOKEN, keep))

    # 3. Pre-release source (prerelease=true ONLY)
    if pre_cfg.get("enabled"):
        owner = pre_cfg.get("owner", mod_owner)
        repo = pre_cfg.get("repo", mod_repo)
        pattern = pre_cfg.get("pattern", "")
        keep = pre_cfg.get("keep", 1)
        print(f"  checking prerelease: {owner}/{repo} (keep={keep})")
        candidates.extend(fetch_releases(owner, repo, pattern, True, GITHUB_TOKEN, keep))

    # 4. Actions source
    if act_cfg.get("enabled"):
        owner = act_cfg.get("owner", mod_owner)
        repo = act_cfg.get("repo", mod_repo)
        wf = act_cfg.get("workflow_file", "")
        pattern = act_cfg.get("pattern", "")
        keep = act_cfg.get("keep", 1)
        print(f"  checking actions: {owner}/{repo} [{wf}] (keep={keep})")
        candidates.extend(fetch_actions(
            owner, repo, wf, pattern, ARTIFACT_PAT or GITHUB_TOKEN, keep
        ))

    if not candidates:
        print("  no candidates found")
        stale = mod_dir / "update.json"
        if stale.exists():
            stale.unlink()
            print("  removed stale update.json")
        return

    # Download, verify checksum, read module.prop
    tmp_dir = REPO_ROOT / ".temp_downloads"
    tmp_dir.mkdir(exist_ok=True)
    verified = []

    for i, c in enumerate(candidates):
        tmp_path = tmp_dir / f"{mod_id}_dl_{i}.zip"
        try:
            download_file(c["download_url"], tmp_path, c["use_headers"])
            props = read_module_prop(tmp_path)
            vc = props.get("versionCode")
            if not vc:
                tmp_path.unlink(missing_ok=True)
                continue

            vc_int = int(vc)
            raw_ver = props.get("version", "")
            clean_ver = clean_version(raw_ver)

            c["versionCode"] = vc_int
            c["clean_version"] = clean_ver
            c["checksum"] = sha256_file(tmp_path)
            c["local_path"] = tmp_path
            c["size"] = tmp_path.stat().st_size
            c["zip_name"] = mmrl_zip_name(clean_ver, vc_int)
            c["md_name"] = mmrl_md_name(clean_ver, vc_int)
            verified.append(c)
        except Exception as e:
            print(f"  [warn] candidate {i} failed: {e}")
            if tmp_path.exists():
                tmp_path.unlink()

    if not verified:
        print("  no valid candidates after verification")
        stale = mod_dir / "update.json"
        if stale.exists():
            stale.unlink()
            print("  removed stale update.json")
        return

    # Dedup by checksum: Release(0) > Pre-release(1) > Action(2)
    by_cs = {}
    for c in verified:
        by_cs.setdefault(c["checksum"], []).append(c)

    deduped = []
    for cs, group in by_cs.items():
        group.sort(key=lambda x: PRIORITY.get(x["source"], 9))
        winner = group[0]
        for loser in group[1:]:
            print(f"  [dedup] same checksum, keeping {winner['source']} over {loser['source']}")
            if loser.get("local_path") and Path(loser["local_path"]).exists():
                Path(loser["local_path"]).unlink()
        deduped.append(winner)

    # Dedup by MMRL filename — ERROR on same-source collisions
    by_fn = {}
    finalists = []
    errors = []
    for c in sorted(deduped, key=lambda x: PRIORITY.get(x["source"], 9)):
        fn = c["zip_name"]
        if fn not in by_fn:
            by_fn[fn] = c
            finalists.append(c)
        else:
            existing = by_fn[fn]
            if existing["source"] == c["source"]:
                # SAME source collision — this is a PATTERN ERROR
                errors.append(
                    f"  [ERROR] same-source filename collision: {fn}\n"
                    f"    kept:     {existing['checksum'][:16]}... from {existing.get('download_url', '?')}\n"
                    f"    discarded: {c['checksum'][:16]}... from {c.get('download_url', '?')}\n"
                    f"    → Pattern is too broad — narrowing required"
                )
                # Keep the one with higher priority (already sorted), discard the other
                if c.get("local_path") and Path(c["local_path"]).exists():
                    Path(c["local_path"]).unlink()
            else:
                # Different source — normal dedup, log and keep higher priority
                print(f"  [dedup] cross-source collision {fn}, keeping {existing['source']} over {c['source']}")
                if c.get("local_path") and Path(c["local_path"]).exists():
                    Path(c["local_path"]).unlink()

    # Print all errors
    if errors:
        for err in errors:
            print(err)

    # Split into pools
    rel_pool = sorted(
        [c for c in finalists if c["source"] == "release" and not c.get("is_static")],
        key=lambda x: x["versionCode"], reverse=True
    )
    pre_pool = sorted(
        [c for c in finalists if c["source"] == "pre-release"],
        key=lambda x: x["versionCode"], reverse=True
    )
    act_pool = sorted(
        [c for c in finalists if c["source"] == "action"],
        key=lambda x: x["versionCode"], reverse=True
    )
    stat_pool = sorted(
        [c for c in finalists if c.get("is_static")],
        key=lambda x: x["versionCode"], reverse=True
    )

    # Retention: keep: is absolute cap
    keep_static = stat_cfg.get("keep", 1) if stat_cfg.get("enabled") else 0
    retained_static = stat_pool[:keep_static]

    keep_rel = rel_cfg.get("keep", 1) if rel_cfg.get("enabled") else 0
    if rel_cfg.get("preserve") and rel_pool:
        keep_rel = max(keep_rel, 1)
    retained_rel = rel_pool[:keep_rel]

    keep_pre = pre_cfg.get("keep", 0) if pre_cfg.get("enabled") else 0
    retained_pre = pre_pool[:keep_pre]

    keep_act = act_cfg.get("keep", 0) if act_cfg.get("enabled") else 0
    retained_act = act_pool[:keep_act]

    retained = retained_static + retained_rel + retained_pre + retained_act

    if not retained:
        print("  nothing to retain")
        stale = mod_dir / "update.json"
        if stale.exists():
            stale.unlink()
            print("  removed stale update.json")
        return

    current = max(retained, key=lambda x: x["versionCode"])

    # Find the latest upstream timestamp across all retained candidates
    last_update_ts = 0
    for c in retained:
        ts = c.get("upstream_ts", 0)
        if ts > last_update_ts:
            last_update_ts = ts

    new_files = set()
    versions = []

    for c in sorted(retained, key=lambda x: x["versionCode"]):
        # Atomic write: copy to temp name, then rename
        dest_zip = mod_dir / c["zip_name"]
        tmp_final = mod_dir / f".tmp_{c['zip_name']}"
        shutil.copy2(c["local_path"], tmp_final)
        os.replace(str(tmp_final), str(dest_zip))
        new_files.add(c["zip_name"])

        dest_md = mod_dir / c["md_name"]
        cl = c.get("changelog", "")
        if c.get("is_static") and cl.startswith("http"):
            try:
                cr = requests.get(cl, timeout=30)
                if cr.status_code == 200:
                    cl = cr.text
            except Exception:
                cl = ""
        dest_md.write_text(cl or "", encoding="utf-8")
        new_files.add(c["md_name"])

        versions.append({
            "timestamp": int(time.time()),
            "version": c["clean_version"],
            "versionCode": c["versionCode"],
            "zipUrl": f"{REPO_RAW}/modules/{mod_id}/{c['zip_name']}",
            "changelog": f"{REPO_RAW}/modules/{mod_id}/{c['md_name']}",
            "size": c["size"],
            "checksum": c["checksum"],
            "source": c["source"],
        })

    # Rebuild update.json from scratch
    update_json = {
        "id": mod_id,
        "timestamp": int(time.time()),
        "versions": sorted(versions, key=lambda x: x["versionCode"]),
    }
    update_path = mod_dir / "update.json"
    update_path.write_text(json.dumps(update_json, indent=2) + "\n", encoding="utf-8")

    # Orphan cleanup: delete anything not in new_files
    keep_names = {"update.json", "track.yaml"}
    for f in mod_dir.iterdir():
        if f.is_file() and f.name not in keep_names and f.name not in new_files:
            print(f"  [orphan] removing {f.name}")
            f.unlink()

    # Rebuild track.yaml from scratch
    track_file = mod_dir / "track.yaml"
    write_track_yaml(track_file, mod_cfg, metadata, last_update_ts)

    # Cleanup temp downloads
    shutil.rmtree(tmp_dir, ignore_errors=True)

    print(f"  done: {len(retained_static)} static + {len(retained_rel)} release + "
          f"{len(retained_pre)} prerelease + {len(retained_act)} action retained")
    print(f"  current: {current['clean_version']} ({current['versionCode']})")


def main():
    if not CONFIG_FILE.exists():
        print(f"ERROR: {CONFIG_FILE} not found")
        sys.exit(1)

    config = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8"))
    gh_h = gh_headers(GITHUB_TOKEN)
    pat_h = gh_headers(ARTIFACT_PAT or GITHUB_TOKEN)

    for mod in config.get("modules", []):
        process_module(mod, gh_h, pat_h)


if __name__ == "__main__":
    main()