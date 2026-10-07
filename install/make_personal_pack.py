#!/usr/bin/env python3
"""Bundle YOUR private files into one zip so a new computer can be set up in one step.

    python install/make_personal_pack.py                 # -> dist/personal-pack.zip (includes data/jobs.db)
    python install/make_personal_pack.py --no-db         # settings only
    python install/make_personal_pack.py --with-resume-profile   # also resumes/profile.* (forks without them)
    python install/make_personal_pack.py --out D:/personal-pack.zip

What goes in (only files that exist):
    config.local.yaml            your name / phone / email / headline / skills / towns / location facts
    data/jobs.db                 optional: your jobs, leads and drafts (consistent snapshot, safe while the app runs)
    resumes/profile.json, profile_text.txt, profile.md
                                 only with --with-resume-profile (this repo already ships resumes/)

Never included: raw resumes (resumes/raw, resumes/text, *.pdf, *.docx), .env / SMTP passwords, logs.
The zip holds personal information: keep it private, never commit or share it.
The installers (install/install.sh, install/install.ps1) find it next to the repo or in Downloads.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACK_VERSION = 1
FILES = ["config.local.yaml"]
RESUME_FILES = ["resumes/profile.json", "resumes/profile_text.txt", "resumes/profile.md"]
DB = "data/jobs.db"
ALLOWED = set(FILES) | set(RESUME_FILES) | {DB}  # the importer refuses anything else


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def snapshot_db(src: Path, dst: Path) -> None:
    """Consistent copy of a live WAL-mode SQLite DB (sqlite3 backup API; read-only on the source)."""
    s = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True, timeout=30)
    d = sqlite3.connect(str(dst))
    try:
        s.backup(d)
        d.execute("PRAGMA journal_mode=DELETE")
        d.commit()
    finally:
        d.close()
        s.close()


def build(out: Path, include_db: bool, include_resume_profile: bool = False) -> dict:
    if not (ROOT / "config.local.yaml").exists():
        sys.exit("config.local.yaml not found - nothing personal to pack (copy config.local.example.yaml first).")
    out.parent.mkdir(parents=True, exist_ok=True)
    manifest = {"pack_version": PACK_VERSION, "created": datetime.now().astimezone().isoformat(timespec="seconds"),
                "files": {}}
    with tempfile.TemporaryDirectory() as tmp:
        staged: list[tuple[str, Path]] = []
        for rel in FILES + (RESUME_FILES if include_resume_profile else []):
            p = ROOT / rel
            if p.is_file():
                staged.append((rel, p))
            else:
                print(f"  (skipping {rel}: not found)")
        if include_db and (ROOT / DB).is_file():
            snap = Path(tmp) / "jobs.db"
            snapshot_db(ROOT / DB, snap)
            staged.append((DB, snap))
        tmp_zip = out.with_suffix(".zip.tmp")
        with zipfile.ZipFile(tmp_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as z:
            for rel, p in staged:
                assert rel in ALLOWED
                manifest["files"][rel] = {"sha256": sha256(p), "bytes": p.stat().st_size}
                z.write(p, rel)
            z.writestr("manifest.json", json.dumps(manifest, indent=2))
        tmp_zip.replace(out)
    return manifest


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default=str(ROOT / "dist" / "personal-pack.zip"), help="output zip path")
    ap.add_argument("--no-db", action="store_true", help="leave out data/jobs.db (jobs, leads, drafts)")
    ap.add_argument("--with-resume-profile", action="store_true",
                    help="also pack resumes/profile.json, profile_text.txt, profile.md (if your repo doesn't ship them)")
    a = ap.parse_args(argv)
    out = Path(a.out).expanduser().resolve()
    m = build(out, include_db=not a.no_db, include_resume_profile=a.with_resume_profile)
    print(f"wrote {out} ({out.stat().st_size // 1024} KB)")
    for rel, info in m["files"].items():
        print(f"  {rel:28s} {info['bytes']:>10,d} bytes")
    print("This zip contains personal information. Keep it private; do NOT commit or share it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
