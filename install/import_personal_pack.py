#!/usr/bin/env python3
"""Unpack personal-pack.zip (made by install/make_personal_pack.py) into this repo.

The pack holds config.local.yaml and optionally data/jobs.db (and resumes/profile.* if it
was made with --with-resume-profile).

    python install/import_personal_pack.py                    # look for it automatically
    python install/import_personal_pack.py C:/Users/me/Downloads/personal-pack.zip
    python install/import_personal_pack.py --find             # just print where it would be found

Search order when no path is given: <repo>/personal-pack.zip, the folder that contains the
repo, <repo>/dist/, then your Downloads folder (newest personal-pack*.zip wins there).

Safe to re-run: a pack that was already imported is skipped entirely, so edits you make
on this computer are never overwritten by the same pack again. A NEW pack (different
contents) replaces changed config/resume files, backing each one up first as
<file>.bak-YYYYmmdd-HHMMSS. data/jobs.db is only copied in when this computer has no jobs
database yet (use --force-db to replace an existing one; it is backed up first).
Only the known file names are extracted; anything else in the zip is ignored.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALLOWED = {"config.local.yaml", "resumes/profile.json", "resumes/profile_text.txt", "resumes/profile.md",
           "data/jobs.db"}
DB = "data/jobs.db"
MARKER = "data/.personal-pack-imported.json"


def candidates(repo: Path = ROOT) -> list[Path]:
    out = [repo / "personal-pack.zip", repo.parent / "personal-pack.zip", repo / "dist" / "personal-pack.zip"]
    dl = Path.home() / "Downloads"
    if dl.is_dir():
        found = sorted(dl.glob("personal-pack*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
        out += found
    return out


def find_pack(repo: Path = ROOT) -> Path | None:
    for c in candidates(repo):
        if c.is_file() and zipfile.is_zipfile(c):
            return c
    return None


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def import_pack(pack: Path, repo: Path = ROOT, force_db: bool = False, dry_run: bool = False,
                force: bool = False) -> dict:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    res = {"written": [], "skipped_same": [], "kept_existing": [], "backed_up": [], "ignored": [],
           "already_imported": False}
    pack_sha = _sha_file(pack)
    marker = repo.joinpath(*MARKER.split("/"))
    try:
        seen = json.loads(marker.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        seen = {}
    if not force and not force_db and seen.get("pack_sha256") == pack_sha:
        res["already_imported"] = seen.get("imported_at") or True
        return res
    with zipfile.ZipFile(pack) as z:
        names = set(z.namelist())
        manifest = json.loads(z.read("manifest.json")) if "manifest.json" in names else {"files": {}}
        for name in sorted(names - {"manifest.json"}):
            norm = name.replace("\\", "/")
            if norm not in ALLOWED:  # also blocks ../ paths and absolute paths
                res["ignored"].append(name)
                continue
            data = z.read(name)
            want = (manifest.get("files") or {}).get(norm, {}).get("sha256")
            if want and want != _sha(data):
                raise SystemExit(f"{pack}: {norm} is corrupt (checksum mismatch); re-create the pack")
            dest = repo.joinpath(*norm.split("/"))
            if dest.exists() and _sha_file(dest) == _sha(data):
                res["skipped_same"].append(norm)
                continue
            if norm == DB and dest.exists() and dest.stat().st_size > 0 and not force_db:
                res["kept_existing"].append(norm)
                continue
            if dry_run:
                res["written"].append(norm + " (dry run)")
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                bak = dest.with_name(dest.name + f".bak-{stamp}")
                shutil.copy2(dest, bak)
                res["backed_up"].append(str(bak.relative_to(repo)))
            tmp = dest.with_name(dest.name + ".tmp-import")
            tmp.write_bytes(data)
            if norm == DB:  # a replaced DB must not be mixed with an old WAL
                for ext in ("-wal", "-shm"):
                    side = dest.with_name(dest.name + ext)
                    if side.exists():
                        side.unlink()
            tmp.replace(dest)
            if os.name != "nt":
                os.chmod(dest, 0o600)  # personal data: owner-only
            res["written"].append(norm)
    if not dry_run:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"pack_sha256": pack_sha, "pack_created": manifest.get("created"),
                                      "imported_at": datetime.now().isoformat(timespec="seconds"),
                                      "source": str(pack)}, indent=2), encoding="utf-8")
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("pack", nargs="?", help="path to personal-pack.zip (default: search)")
    ap.add_argument("--force", action="store_true", help="re-apply a pack even if it was already imported")
    ap.add_argument("--force-db", action="store_true", help="replace an existing data/jobs.db (backed up first)")
    ap.add_argument("--dry-run", action="store_true", help="show what would be written; write nothing")
    ap.add_argument("--find", action="store_true", help="only print the pack that would be used")
    a = ap.parse_args(argv)
    pack = Path(a.pack).expanduser() if a.pack else find_pack()
    if a.find:
        print(pack or "")
        return 0 if pack else 1
    if not pack or not pack.is_file():
        print("No personal-pack.zip found (looked in: " + ", ".join(str(c) for c in candidates()[:4]) + ").")
        return 1
    r = import_pack(pack.resolve(), force_db=a.force_db, dry_run=a.dry_run, force=a.force)
    print(f"Personal pack: {pack}")
    if r["already_imported"]:
        print(f"  already imported ({r['already_imported']}); keeping this computer's files. Use --force to re-apply.")
        return 0
    for k, label in (("written", "installed"), ("skipped_same", "already up to date"),
                     ("kept_existing", "kept this computer's copy (use --force-db to replace)"),
                     ("backed_up", "backup made"), ("ignored", "ignored (not a known file)")):
        for f in r[k]:
            print(f"  {label}: {f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
