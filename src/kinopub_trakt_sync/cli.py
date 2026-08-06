"""Command line interface.

One-off use is `kts sync`; the individual steps (pull, plan, push, verify)
exist for when a stage needs inspecting on its own.
"""

from __future__ import annotations

import asyncio
import logging
from enum import StrEnum
from typing import Annotated

import typer

from . import status as status_module
from . import transform, verify
from .gemini import Gemini, MatcherError
from .kinopub import KinopubClient, KinopubError
from .models import Dump, Plan
from .pull import pull
from .push import PushState, push_history, push_progress, push_watchlist
from .reconcile import Catalog, DecisionCache, reconcile_plan
from .settings import Settings
from .storage import read_json, read_model, write_json, write_model
from .trakt import TraktClient, TraktError
from .verify import VerifyReport

app = typer.Typer(
    help="One-way sync of kino.pub watch data to Trakt.",
    no_args_is_help=True,
    add_completion=False,
)


class Service(StrEnum):
    KINOPUB = "kinopub"
    TRAKT = "trakt"


# -- steps, shared by the individual commands and by `sync` -----------------


async def _plan_step(settings: Settings, dump: Dump) -> Plan:
    plan = transform.build_plan(dump)
    catalog_cache = read_json(settings.paths.trakt_cache, default={})
    decision_cache = read_json(settings.paths.reconcile_cache, default={})
    async with TraktClient(settings) as client:
        plan = await reconcile_plan(
            plan,
            dump,
            Catalog(client, catalog_cache),
            Gemini(settings),
            DecisionCache(decision_cache),
        )
    write_json(settings.paths.trakt_cache, catalog_cache)
    write_json(settings.paths.reconcile_cache, decision_cache)
    write_model(settings.paths.plan, plan)

    for note in plan.notes:
        print(f"reconcile: {note}")
    print(transform.format_summary(plan))
    return plan


async def _push_step(
    settings: Settings,
    plan: Plan,
    *,
    history: bool,
    progress: bool,
    watchlist: bool,
    dry_run: bool,
) -> None:
    state = PushState.load(settings.paths.push_state)
    async with TraktClient(settings) as client:
        for selected, action in (
            (history, push_history),
            (progress, push_progress),
            (watchlist, push_watchlist),
        ):
            if selected:
                await action(plan, client, state, settings.paths.push_state, dry_run=dry_run)


async def _verify_step(settings: Settings, plan: Plan, *, fix: bool) -> VerifyReport:
    async with TraktClient(settings) as client:
        report = await verify.build_report(plan, client)
        print(verify.format_report(report))
        write_model(settings.paths.verify_report, report)

        if not report.problem_count:
            print("account matches the plan exactly")
            verify.adopt_plan_as_state(plan, settings.paths.push_state)
        elif fix:
            await verify.apply_fixes(plan, report, client, settings.paths.push_state)
            print("fixes applied, re-run verify to confirm")
        return report


def _load_dump(settings: Settings) -> Dump:
    dump = read_model(settings.paths.dump, Dump)
    if dump is None:
        raise typer.BadParameter("no dump found, run: kts pull")
    return dump


def _load_plan(settings: Settings) -> Plan:
    plan = read_model(settings.paths.plan, Plan)
    if plan is None:
        raise typer.BadParameter("no plan found, run: kts plan")
    return plan


# -- commands ---------------------------------------------------------------


@app.callback()
def main_options(
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Log API-level detail.")] = False,
) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )


@app.command()
def auth(service: Annotated[Service, typer.Argument(help="Which service to authorize.")]) -> None:
    """Authorize a service through its device-code flow."""

    async def run() -> None:
        settings = Settings()
        if service is Service.KINOPUB:
            async with KinopubClient(settings) as client:
                await client.device_auth()
        else:
            async with TraktClient(settings) as client:
                await client.device_auth()

    asyncio.run(run())


@app.command(name="pull")
def pull_command() -> None:
    """Dump watch history and progress from kino.pub into data/."""
    asyncio.run(pull(Settings()))


@app.command(name="plan")
def plan_command() -> None:
    """Build a sync plan from the dump, reconciling Trakt episode identities."""

    async def run() -> None:
        settings = Settings()
        await _plan_step(settings, _load_dump(settings))
        print(f"plan saved: {settings.paths.plan}")

    asyncio.run(run())


@app.command(name="push")
def push_command(
    history: Annotated[bool, typer.Option("--history", help="Watched movies and episodes.")] = False,
    progress: Annotated[
        bool, typer.Option("--progress", help="Playback position of unfinished items.")
    ] = False,
    watchlist: Annotated[bool, typer.Option("--watchlist", help="kino.pub watchlist shows.")] = False,
    all_sections: Annotated[bool, typer.Option("--all", help="History, progress and watchlist.")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Only report what would be pushed.")] = False,
) -> None:
    """Apply the plan to Trakt."""
    if not (history or progress or watchlist or all_sections):
        raise typer.BadParameter("select --history / --progress / --watchlist / --all")

    async def run() -> None:
        settings = Settings()
        await _push_step(
            settings,
            _load_plan(settings),
            history=history or all_sections,
            progress=progress or all_sections,
            watchlist=watchlist or all_sections,
            dry_run=dry_run,
        )

    asyncio.run(run())


@app.command(name="verify")
def verify_command(
    fix: Annotated[bool, typer.Option("--fix", help="Remove wrong events and push missing ones.")] = False,
) -> None:
    """Check the Trakt account against the plan, entry by entry."""

    async def run() -> None:
        settings = Settings()
        await _verify_step(settings, _load_plan(settings), fix=fix)

    asyncio.run(run())


@app.command(name="sync")
def sync_command(
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Pull and plan, but write nothing to Trakt.")
    ] = False,
) -> None:
    """Run the whole pipeline: pull, plan, push, verify."""

    async def run() -> None:
        settings = Settings()
        dump = await pull(settings)
        plan = await _plan_step(settings, dump)
        await _push_step(settings, plan, history=True, progress=True, watchlist=True, dry_run=dry_run)
        if dry_run:
            print("dry run: nothing was written to Trakt")
            return
        report = await _verify_step(settings, plan, fix=False)
        if report.problem_count:
            # Surfaced, never auto-repaired: fixing deletes history events.
            print("run `kts verify --fix` to reconcile the differences above")

    asyncio.run(run())


@app.command(name="status")
def status_command() -> None:
    """Report local sync state: dump age, pending pushes, authorization."""
    print(status_module.format_status(status_module.collect(Settings())))


def main() -> None:
    try:
        app()
    except (KinopubError, TraktError, MatcherError) as exc:
        # Outside click's runtime, so exit directly: typer.Exit would surface
        # as an uncaught exception instead of an exit code.
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
