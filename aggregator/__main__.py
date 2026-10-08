"""CLI: python -m aggregator {fetch,serve,token,verify-slugs,rescore,stats,qualify,followups,send-approved,auto,enrich,snapshot}"""
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
    for noisy in ("httpx", "httpcore", "urllib3", "primp", "ddgs"):
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
    import os

    import uvicorn

    from . import access

    host = "0.0.0.0" if args.lan else (args.host or cfg["server"].get("host") or "127.0.0.1")
    port = args.port or cfg["server"]["port"]
    if args.no_token:
        os.environ["AGGREGATOR_NO_TOKEN"] = "1"
    os.environ["AGGREGATOR_BIND_HOST"] = host
    if access.is_loopback(host):
        print(f"Job Aggregator UI: http://localhost:{port}  (this computer only; "
              f"use --lan to allow your phone on the same Wi-Fi)", flush=True)
    else:
        token = access.resolve_token(cfg)
        if not token and not access.disabled():
            token = access.create_token_file()
            print(f"Created a new access token in {access.TOKEN_FILE}", flush=True)
        ips = access.lan_ips() if host in ("0.0.0.0", "::") else [host]
        print("=" * 64, flush=True)
        print("LAN mode: other devices on your network can reach this UI.", flush=True)
        for ip in ips or ["<this computer's IP>"]:
            print(f"  Phone app server address:  http://{ip}:{port}", flush=True)
        if token:
            print(f"  Access token:              {token}   ({access.token_source(cfg)})", flush=True)
        else:
            print("  WARNING: no access token: anyone on this network can use the UI.", flush=True)
        print(f"  Also shown at http://localhost:{port}/phone on this computer.", flush=True)
        print("  Allow TCP port %d through the firewall (private/home network only)." % port, flush=True)
        print("=" * 64, flush=True)
    uvicorn.run("aggregator.web:app", host=host, port=port, log_level="info")


def cmd_token(args, cfg):
    from . import access

    if args.new:
        if access.resolve_token(cfg) and access.token_source(cfg) != access.TOKEN_FILE:
            print(f"Token comes from {access.token_source(cfg)}; change it there.")
            return 1
        t = access.create_token_file(force=True)
        print(f"New access token: {t}\nRestart the server (serve --lan) and re-enter it in the phone app.")
        return 0
    t = access.resolve_token(cfg)
    if not t:
        t = access.create_token_file()
        print(f"Created {access.TOKEN_FILE}")
    print(f"Access token: {t}   ({access.token_source(cfg)})")
    port = cfg["server"]["port"]
    for ip in access.lan_ips():
        print(f"Phone app server address (when running `serve --lan`): http://{ip}:{port}")
    return 0


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


def cmd_redraft(args, cfg):
    from . import db, followups

    conn = db.connect(cfg)
    for jid in args.job_ids:
        n = followups.redraft(conn, cfg, jid)
        print(f"{jid}: rewrote {n} unedited draft(s)")
        for f in conn.execute("SELECT touch, generator, subject FROM followups WHERE job_id=? ORDER BY touch", (jid,)):
            print(f"  touch {f['touch']} [{f['generator']}] {f['subject']}")


def cmd_enrich(args, cfg):
    from . import db, enrich

    conn = db.connect(cfg)
    if args.job_ids:
        ids = args.job_ids
    else:
        ids = enrich.targets(conn, cfg, top=args.top if args.top is not None else (50 if args.all else 0), leads=True)
    budget = args.budget if args.budget is not None else (None if not (args.all or args.job_ids) else 3600)
    print(f"Enriching {len(ids)} job(s){' (forced re-crawl)' if args.force else ''} ...")
    st = enrich.enrich_jobs(conn, cfg, ids, budget_s=budget, max_companies=10_000 if (args.all or args.job_ids) else None,
                            force=args.force)
    for jid in ids:
        b = enrich.brief(conn, jid)
        if not b:
            print(f"\n{jid}: not found")
            continue
        lc = b.get("lead_contact") or {}
        print(f"\n{b['company']}  [{b.get('status') or '-'}]  job {jid[:12]}")
        for line in enrich.md_lines(b, indent="  "):
            print(line)
        if lc:
            print(f"  - lead contact: {lc.get('contact_email') or 'NONE'} ({lc.get('contact_source') or '-'})")
    print("\n" + json.dumps({k: v for k, v in st.items()}, indent=2, default=str))
    return 0


def cmd_snapshot(args, cfg):
    from .snapshot import main_cli

    return main_cli(args, cfg)


def cmd_auto(args, cfg):
    from .auto import main_cli

    return main_cli(args, cfg)


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

    s = sub.add_parser("serve", help="run the web UI (default: http://localhost:8765, this computer only)")
    s.add_argument("--host", help="bind address (default server.host = 127.0.0.1; 0.0.0.0 = every network interface)")
    s.add_argument("--port", type=int)
    s.add_argument("--lan", action="store_true",
                   help="same as --host 0.0.0.0: let your phone (same Wi-Fi) connect; requires the access token")
    s.add_argument("--no-token", action="store_true",
                   help="LAN mode WITHOUT an access token (anyone on the network can use the UI)")
    s.set_defaults(func=cmd_serve)

    tk = sub.add_parser("token", help="show (or create) the LAN access token for the phone app")
    tk.add_argument("--new", action="store_true", help="replace data/access_token.txt with a fresh token")
    tk.set_defaults(func=cmd_token)

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

    rp = sub.add_parser("redraft", help="rewrite unedited drafts of qualified job(s) (e.g. with Ollama); never sends")
    rp.add_argument("job_ids", nargs="+")
    rp.set_defaults(func=cmd_redraft)

    ap = sub.add_parser("auto", help="unattended run: fetch, rescore, auto-qualify strong new matches (drafts only), write digest")
    ap.add_argument("--no-fetch", action="store_true", help="skip fetching (use what's in the DB)")
    ap.add_argument("--no-qualify", action="store_true", help="don't qualify anything (digest only)")
    ap.set_defaults(func=cmd_auto)

    ep = sub.add_parser("enrich", help="look up company info (website, careers/apply link, HR email + source) for leads/jobs; never sends")
    ep.add_argument("job_ids", nargs="*", help="job ids (default: all active leads)")
    ep.add_argument("--all", action="store_true", help="all active leads + the top 50 scored jobs, no time cap")
    ep.add_argument("--top", type=int, help="also enrich the top N scored jobs")
    ep.add_argument("--force", action="store_true", help="re-crawl companies even if enriched in the last enrich.cache_days")
    ep.add_argument("--budget", type=float, help="time budget in seconds (default: enrich.max_seconds_per_run; none with --all)")
    ep.set_defaults(func=cmd_enrich)

    sn = sub.add_parser("snapshot", help="write ONE self-contained phone-friendly HTML dashboard (no server needed); never sends")
    sn.add_argument("--out", help="output .html path (default: dist/Job-Dashboard-YYYY-MM-DD.html)")
    sn.add_argument("--publish", action="store_true",
                    help="also force-push it as index.html to the orphan gh-pages branch (GitHub Pages); main is never touched")
    sn.set_defaults(func=cmd_snapshot)

    sub.add_parser("rescore", help="recompute relevance scores after editing scoring.profile").set_defaults(func=cmd_rescore)
    sub.add_parser("stats", help="show DB counts and last run summary").set_defaults(func=cmd_stats)

    args = p.parse_args(argv)
    _setup_logging(args.verbose)
    cfg = load_config(args.config)
    return args.func(args, cfg) or 0


if __name__ == "__main__":
    sys.exit(main())
