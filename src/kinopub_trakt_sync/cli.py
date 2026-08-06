import argparse
import sys

from . import config, push, transform
from .kinopub import KinopubClient
from .pull import pull
from .trakt import TraktClient


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="kts", description="One-way sync of kino.pub watch data to Trakt"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    auth = sub.add_parser("auth", help="authorize a service (device code flow)")
    auth.add_argument("service", choices=["kinopub", "trakt"])

    sub.add_parser("pull", help="dump watch data from kino.pub into data/")
    sub.add_parser("plan", help="build a sync plan from the dump and print a summary")

    push_cmd = sub.add_parser("push", help="apply the plan to Trakt")
    push_cmd.add_argument("--history", action="store_true", help="watched movies and episodes")
    push_cmd.add_argument("--progress", action="store_true", help="playback progress of unfinished items")
    push_cmd.add_argument("--watchlist", action="store_true", help="kino.pub watchlist shows")
    push_cmd.add_argument("--all", action="store_true", help="history + progress + watchlist")
    push_cmd.add_argument("--dry-run", action="store_true", help="only report what would be pushed")

    args = parser.parse_args()

    if args.command == "auth":
        if args.service == "kinopub":
            KinopubClient().device_auth()
        else:
            TraktClient().device_auth()

    elif args.command == "pull":
        pull()

    elif args.command == "plan":
        if not config.DUMP_FILE.exists():
            sys.exit("no dump found, run: kts pull")
        plan = transform.build_plan(config.load_json(config.DUMP_FILE))
        config.save_json(config.PLAN_FILE, plan)
        transform.print_summary(plan)
        print(f"plan saved: {config.PLAN_FILE}")

    elif args.command == "push":
        if not (args.history or args.progress or args.watchlist or args.all):
            sys.exit("nothing selected: use --history / --progress / --watchlist / --all")
        if not config.PLAN_FILE.exists():
            sys.exit("no plan found, run: kts plan")
        plan = config.load_json(config.PLAN_FILE)
        client = TraktClient()
        if args.history or args.all:
            push.push_history(plan, client, args.dry_run)
        if args.progress or args.all:
            push.push_progress(plan, client, args.dry_run)
        if args.watchlist or args.all:
            push.push_watchlist(plan, client, args.dry_run)


if __name__ == "__main__":
    main()
