"""CLI: python -m aggregator {fetch,serve,verify-slugs,rescore,stats,qualify,followups,send-approved}"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

import yaml

from .config import load_companies, load_config, resolve


def _setup_logging(verbose: bool):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def cmd_fetch(args, cfg):
    from .fetcher import run_fetch

    only = [s.strip() for s in args.only.split(",")] if args.only else None
    if args.no_jobspy:
        cfg["jobspy"]["enabled"] = False
    if args.no_ats:
        cfg["ats"]["enabled"] = False
    tracks = [t.strip() for t in args.track.split(",")] if args.track else None
    summary = run_fetch(cfg, only=only, only_tracks=tracks)
    print("\n===== FETCH SUMMARY =====")
    print(json.dumps({k: v for k, v in summary.items() if k != "per_company"}, indent=2))
    if args.companies:
        print(json.dumps(summary["per_company"], indent=2))


def cmd_serve(args, cfg):
    import uvicorn

    host = args.host or cfg["server"]["host"]
    port = args.port or cfg["server"]["port"]
    uvicorn.run("aggregator.web:app", host=host, port=port, log_level="info")


def cmd_verify(args, cfg):
    from .sources.ats import verify_slugs

    path = cfg["tracks"]["remote_ai"]["companies_file"] if args.track == "remote_ai" else None
    companies = load_companies(cfg, path)
    results = asyncio.run(verify_slugs(companies))
    bad = set()
    for ats, slug, status, n in sorted(results):
        ok = status == 200
        print(f"{'OK  ' if ok else 'FAIL'} {ats:10s} {slug:24s} HTTP {status}  {n} postings")
        if not ok:
            bad.add((ats, slug))
    print(f"\n{len(results) - len(bad)}/{len(results)} slugs OK")
    if bad and args.prune:
        p = resolve(path or cfg["ats"]["companies_file"])
        data = yaml.safe_load(p.read_text()) or {}
        for ats in list(data):
            data[ats] = [c for c in data[ats] or [] if (ats, c["slug"] if isinstance(c, dict) else c) not in bad]
        p.write_text(yaml.safe_dump(data, sort_keys=False))
        print(f"Pruned {len(bad)} failing slugs from {p}")
    return 1 if bad and not args.prune else 0


def cmd_rescore(args, cfg):
    from . import db
    from .matching import rescore_all

    n = rescore_all(cfg, db.connect(cfg))
    print(f"Rescored {n} jobs")


def cmd_stats(args, cfg):
    from . import db

    conn = db.connect(cfg)
    print("Jobs by source:", dict(conn.execute("SELECT source, COUNT(*) FROM jobs GROUP BY source").fetchall()))
    print("Total:", conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
    for t in ("atlanta", "remote_ai"):
        n = conn.execute("SELECT COUNT(*) FROM jobs WHERE ',' || track || ',' LIKE ?", (f"%,{t},%",)).fetchone()[0]
        print(f"Track {t}: {n}")
    print("remote_ai by remote_region:", dict(conn.execute(
        "SELECT remote_region, COUNT(*) FROM jobs WHERE ',' || track || ',' LIKE '%,remote_ai,%' GROUP BY 1 ORDER BY 2 DESC").fetchall()))
    row = conn.execute("SELECT summary_json FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
    if row:
        print("Last run:", json.dumps(json.loads(row[0]), indent=2))


def cmd_qualify(args, cfg):
    from datetime import date

    from . import db, followups

    conn = db.connect(cfg)
    start = date.fromisoformat(args.start) if args.start else None
    for jid in args.job_ids:
        followups.qualify(conn, cfg, jid, by="manual", start=start)
        job = conn.execute("SELECT title, company, location FROM jobs WHERE id=?", (jid,)).fetchone()
        print(f"\nQualified {jid}: {job['title']} @ {job['company']} ({job['location']})")
        for f in conn.execute("SELECT touch, scheduled_for, status, subject, body FROM followups WHERE job_id=? ORDER BY touch", (jid,)):
            print(f"  touch {f['touch']}  {f['scheduled_for']}  [{f['status']}]  {f['subject']}")
            if args.show:
                print("    " + f["body"].replace("\n", "\n    ") + "\n")


def cmd_followups(args, cfg):
    from . import db, followups

    q = followups.queue(db.connect(cfg))
    for section in ("due", "upcoming") + (("done",) if args.all else ()):
        print(f"\n== {section} ({len(q[section])}) ==")
        for f in q[section]:
            to = f["contact_email"] or "NEEDS CONTACT"
            print(f"  {f['scheduled_for']}  touch {f['touch']}  {f['status']:8s}  {f['company'][:24]:24s}  {f['title'][:40]:40s}  -> {to}")


def cmd_send(args, cfg):
    from . import db, sender

    res = sender.send_approved(db.connect(cfg), cfg, dry_run=args.dry_run)
    if res["blocked_by"]:
        print("NOT SENDING. Blocked by:")
        for p in res["blocked_by"]:
            print("  -", p)
    print(f"{len(res['items'])} approved follow-up(s) due; sent {res['sent']}{' (dry run)' if res.get('dry_run') else ''}")
    for it in res["items"]:
        print(f"  {it['scheduled_for']} touch {it['touch']} -> {it['contact_email']}: {it['subject']}")
    for e in res.get("errors") or []:
        print("  ERROR", e)
    return 2 if res["blocked_by"] and not args.dry_run else 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m aggregator", description="Open-source job aggregator")
    p.add_argument("-c", "--config", help="path to config.yaml (default: ./config.yaml or $AGGREGATOR_CONFIG)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="fetch jobs from all enabled sources into the DB")
    f.add_argument("--only", help="comma list of sources, e.g. greenhouse,lever,indeed")
    f.add_argument("--no-jobspy", action="store_true", help="skip JobSpy boards")
    f.add_argument("--no-ats", action="store_true", help="skip Greenhouse/Lever/Ashby")
    f.add_argument("--companies", action="store_true", help="also print per-company counts")
    f.add_argument("--track", help="comma list of tracks to run: atlanta,remote_ai (default: all enabled)")
    f.set_defaults(func=cmd_fetch)

    s = sub.add_parser("serve", help="run the web UI")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.set_defaults(func=cmd_serve)

    v = sub.add_parser("verify-slugs", help="check every company slug returns HTTP 200")
    v.add_argument("--prune", action="store_true", help="remove failing slugs from companies.yaml")
    v.add_argument("--track", choices=["atlanta", "remote_ai"], default="atlanta",
                   help="which seed list: companies.yaml (atlanta) or companies_ai.yaml (remote_ai)")
    v.set_defaults(func=cmd_verify)

    qp = sub.add_parser("qualify", help="mark job(s) qualified and generate 3 follow-up drafts (never sends)")
    qp.add_argument("job_ids", nargs="+")
    qp.add_argument("--start", help="anchor date YYYY-MM-DD (default today)")
    qp.add_argument("--show", action="store_true", help="print draft bodies")
    qp.set_defaults(func=cmd_qualify)

    fp = sub.add_parser("followups", help="list the follow-up draft queue")
    fp.add_argument("--all", action="store_true", help="include sent/skipped/replied")
    fp.set_defaults(func=cmd_followups)

    sp = sub.add_parser("send-approved", help="send APPROVED, due follow-ups via SMTP (explicit, opt-in)")
    sp.add_argument("--dry-run", action="store_true", help="list what would be sent; send nothing")
    sp.set_defaults(func=cmd_send)

    sub.add_parser("rescore", help="recompute relevance scores after editing scoring.profile").set_defaults(func=cmd_rescore)
    sub.add_parser("stats", help="show DB counts and last run summary").set_defaults(func=cmd_stats)

    args = p.parse_args(argv)
    _setup_logging(args.verbose)
    cfg = load_config(args.config)
    return args.func(args, cfg) or 0


if __name__ == "__main__":
    sys.exit(main())
