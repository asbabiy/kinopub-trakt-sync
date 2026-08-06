import argparse
import asyncio
import sys

from . import config, push, transform, verify
from .gemini import Gemini
from .kinopub import KinopubClient
from .pull import pull
from .reconcile import Catalog, reconcile_plan
from .trakt import TraktClient


async def _plan() -> None:
    if not config.DUMP_FILE.exists():
        sys.exit("no dump found, run: kts pull")
    dump = config.load_json(config.DUMP_FILE)
    plan = transform.build_plan(dump)
    cache = config.load_json(config.TRAKT_CACHE_FILE) if config.TRAKT_CACHE_FILE.exists() else {}
    catalog = Catalog(TraktClient(), cache)
    plan = await reconcile_plan(plan, dump, catalog, Gemini())
    config.save_json(config.TRAKT_CACHE_FILE, cache)
    for note in plan.get("reconcile_notes", []):
        print(f"reconcile: {note}")
    config.save_json(config.PLAN_FILE, plan)
    transform.print_summary(plan)
    print(f"plan saved: {config.PLAN_FILE}")


async def _push(args) -> None:
    if not (args.history or args.progress or args.watchlist or args.all):
        sys.exit("nothing selected: use --history / --progress / --watchlist / --all")
    if not config.PLAN_FILE.exists():
        sys.exit("no plan found, run: kts plan")
    plan = config.load_json(config.PLAN_FILE)
    client = TraktClient()
    if args.history or args.all:
        await push.push_history(plan, client, args.dry_run)
    if args.progress or args.all:
        await push.push_progress(plan, client, args.dry_run)
    if args.watchlist or args.all:
        await push.push_watchlist(plan, client, args.dry_run)


async def _verify(args) -> None:
    if not config.PLAN_FILE.exists():
        sys.exit("no plan found, run: kts plan")
    plan = config.load_json(config.PLAN_FILE)
    client = TraktClient()
    report = await verify.build_report(plan, client)
    verify.print_report(report)
    config.save_json(config.VERIFY_REPORT_FILE, report)
    problems = sum(
        len(report[k])
        for k in (
            "missing_movies",
            "missing_episodes",
            "ts_mismatch",
            "extra_events",
            "progress_missing",
            "progress_mismatch",
        )
    )
    if args.fix and problems:
        await verify.apply_fixes(plan, report, client)
        print("fixes applied, re-run verify to confirm")
    elif not problems:
        print("account matches the plan exactly")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="kts", description="One-way sync of kino.pub watch data to Trakt"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    auth = sub.add_parser("auth", help="authorize a service (device code flow)")
    auth.add_argument("service", choices=["kinopub", "trakt"])

    sub.add_parser("pull", help="dump watch data from kino.pub into data/")
    sub.add_parser("plan", help="build a sync plan (with Trakt identity reconciliation) and print a summary")

    ver = sub.add_parser("verify", help="element-wise check of the Trakt account against the plan")
    ver.add_argument("--fix", action="store_true", help="remove wrong events and push missing ones")

    push_cmd = sub.add_parser("push", help="apply the plan to Trakt")
    push_cmd.add_argument("--history", action="store_true", help="watched movies and episodes")
    push_cmd.add_argument("--progress", action="store_true", help="playback progress of unfinished items")
    push_cmd.add_argument("--watchlist", action="store_true", help="kino.pub watchlist shows")
    push_cmd.add_argument("--all", action="store_true", help="history + progress + watchlist")
    push_cmd.add_argument("--dry-run", action="store_true", help="only report what would be pushed")

    args = parser.parse_args()

    if args.command == "auth":
        if args.service == "kinopub":
            asyncio.run(KinopubClient().device_auth())
        else:
            asyncio.run(TraktClient().device_auth())
    elif args.command == "pull":
        asyncio.run(pull())
    elif args.command == "plan":
        asyncio.run(_plan())
    elif args.command == "push":
        asyncio.run(_push(args))
    elif args.command == "verify":
        asyncio.run(_verify(args))


if __name__ == "__main__":
    main()
