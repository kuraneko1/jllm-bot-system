"""Command-line inspection of installed plugins and profiles."""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from collections.abc import Sequence
from pathlib import Path

from jllm.kernel import Kernel, ProfileError, discover, load_profile, resolve


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jllm")
    parser.add_argument("--profile", dest="profile_option", type=Path, default=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest="command", required=True)

    plugins = commands.add_parser("plugins", help="Inspect installed plugins")
    plugins.add_subparsers(dest="action", required=True).add_parser("list")

    run = commands.add_parser("run", help="Run a profile until interrupted")
    doctor = commands.add_parser("doctor", help="Check a profile and its dependencies")
    config = commands.add_parser("config", help="Inspect profile configuration")
    validate = config.add_subparsers(dest="action", required=True).add_parser("validate")
    for command in (run, doctor, config, validate):
        command.add_argument(
            "--profile", dest="profile_option", type=Path, default=argparse.SUPPRESS
        )
    for command in (run, doctor, validate):
        command.add_argument("profile", nargs="?", type=Path, default=None)

    args = parser.parse_args(argv)
    try:
        if args.command == "plugins":
            for plugin_id, manifest in sorted(discover().items()):
                print(f"{plugin_id}\t{manifest.version}")
            return 0

        profile_option = getattr(args, "profile_option", None)
        if args.profile and profile_option:
            parser.error("specify the profile either as an argument or with --profile")
        profile_path = profile_option or args.profile or os.environ.get("JLLM_PROFILE")
        if not profile_path:
            print(
                "jllm: specify a profile with --profile, an argument, or JLLM_PROFILE",
                file=sys.stderr,
            )
            return 1
        plan = resolve(load_profile(Path(profile_path)), discover())
        if args.command == "run":
            return asyncio.run(_run(plan))
        if args.command == "config":
            print("Configuration valid")
            return 0

        blocked = False
        for instance_id, node in sorted(plan.nodes.items()):
            reason = f": {node.reason}" if node.reason else ""
            print(f"{instance_id}\t{node.state}{reason}")
            blocked |= node.state == "BLOCKED"
        return 1 if blocked else 0
    except (ProfileError, OSError, ValueError) as exc:
        print(f"jllm: {exc}", file=sys.stderr)
        return 1


async def _activate(plan) -> None:
    activations = [
        node.plugin.activate()
        for node in plan.nodes.values()
        if node.state == "RUNNING" and hasattr(node.plugin, "activate")
    ]
    if activations:
        await asyncio.gather(*activations)


async def _run(plan) -> int:
    kernel = Kernel(plan)
    loop = asyncio.get_running_loop()
    stopped = asyncio.Event()
    signals = (signal.SIGINT, signal.SIGTERM)
    for sig in signals:
        loop.add_signal_handler(sig, stopped.set)
    startup = asyncio.create_task(kernel.start())
    activation = None
    shutdown = asyncio.create_task(stopped.wait())
    exit_code = 0
    try:
        done, _ = await asyncio.wait((startup, shutdown), return_when=asyncio.FIRST_COMPLETED)
        if shutdown in done and not startup.done():
            startup.cancel()
            await asyncio.gather(startup, return_exceptions=True)
        else:
            await startup
            activation = asyncio.create_task(_activate(plan))
            done, _ = await asyncio.wait(
                (activation, shutdown),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if shutdown in done and not activation.done():
                activation.cancel()
                await asyncio.gather(activation, return_exceptions=True)
            else:
                await activation
    except Exception:
        print("jllm: plugin startup failed", file=sys.stderr)
        exit_code = 1
    finally:
        for task in (startup, activation, shutdown):
            if task is not None:
                task.cancel()
        await asyncio.gather(
            *(task for task in (startup, activation, shutdown) if task is not None),
            return_exceptions=True,
        )
        try:
            await kernel.stop()
        except Exception:
            print("jllm: plugin shutdown failed", file=sys.stderr)
            exit_code = 1
        for sig in signals:
            loop.remove_signal_handler(sig)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
