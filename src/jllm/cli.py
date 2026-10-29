"""Command-line inspection of installed plugins and profiles."""

from __future__ import annotations

import argparse
import asyncio
import io
import logging
import os
import signal
import sys
from collections.abc import Sequence
from pathlib import Path

from dotenv import load_dotenv
from dotenv.parser import parse_stream

from jllm.kernel import Kernel, ProfileError, discover, load_profile, resolve


def _load_env_files(env_file: Path | None, policy_file: Path | None) -> None:
    streams = []
    try:
        for path in (env_file, policy_file):
            if path is None:
                streams.append(None)
                continue
            stream = io.StringIO(path.read_text(encoding="utf-8"))
            if any(binding.error for binding in parse_stream(stream)):
                raise ValueError
            stream.seek(0)
            streams.append(stream)
    except (OSError, UnicodeError, ValueError):
        raise ValueError("cannot read or parse environment file") from None
    for stream, override in zip(streams, (False, True), strict=True):
        if stream is not None:
            load_dotenv(stream=stream, override=override)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jllm")
    parser.add_argument("--profile", dest="profile_option", type=Path, default=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest="command", required=True)

    plugins = commands.add_parser("plugins", help="Inspect installed plugins")
    plugins.add_subparsers(dest="action", required=True).add_parser("list")

    run = commands.add_parser("run", help="Run a profile until interrupted")
    doctor = commands.add_parser("doctor", help="Check a profile and its dependencies")
    graph = commands.add_parser("graph", help="Print the resolved plugin map")
    graph.add_argument("--format", choices=("mermaid", "dot"), default="mermaid")
    config = commands.add_parser("config", help="Inspect profile configuration")
    validate = config.add_subparsers(dest="action", required=True).add_parser("validate")
    for command in (parser, run, doctor, config, validate):
        command.add_argument(
            "--env-file", type=Path, default=argparse.SUPPRESS,
            help="Load an explicit dotenv file without overriding the environment",
        )
        command.add_argument(
            "--policy-file", type=Path, default=argparse.SUPPRESS,
            help="Load an explicit trusted policy dotenv file, overriding the environment",
        )
    for command in (run, doctor, graph, config, validate):
        command.add_argument(
            "--profile", dest="profile_option", type=Path, default=argparse.SUPPRESS
        )
    for command in (run, doctor, graph, validate):
        command.add_argument("profile", nargs="?", type=Path, default=None)

    args = parser.parse_args(argv)
    try:
        _load_env_files(getattr(args, "env_file", None), getattr(args, "policy_file", None))
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
            return asyncio.run(_run(plan, Path(profile_path)))
        if args.command == "config":
            print("Configuration valid")
            return 0
        if args.command == "graph":
            _print_graph(plan, args.format)
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


def _graph_id(instance_id: str) -> str:
    return "".join(char if char.isalnum() else "_" for char in instance_id)


def _print_graph(plan, fmt: str) -> None:
    """解決済みの plan を依存グラフとして出す（Mermaid / Graphviz）。"""

    if fmt == "dot":
        print("digraph plugins {")
        print('  rankdir="LR";')
        for key in plan.order:
            node = plan.nodes[key]
            shape = ", shape=hexagon" if node.critical else ""
            print(f'  "{key}" [label="{key}"{shape}];')
        for key in plan.order:
            node = plan.nodes[key]
            for capability, targets in sorted(node.bindings.items()):
                for target in targets:
                    print(f'  "{target}" -> "{key}" [label="{capability}"];')
        print("}")
        return

    print("graph LR")
    print("  %% 太枠 = critical（失敗すると起動を中止）")
    for key in plan.order:
        node = plan.nodes[key]
        style = ":::critical" if node.critical else ""
        print(f'  {_graph_id(key)}["{key}"]{style}')
    for key in plan.order:
        node = plan.nodes[key]
        for capability, targets in sorted(node.bindings.items()):
            for target in targets:
                print(f"  {_graph_id(target)} -->|{capability}| {_graph_id(key)}")
    print("  classDef critical stroke-width:3px")


async def _activate(plan) -> None:
    activations = [
        node.plugin.activate()
        for node in plan.nodes.values()
        if node.state == "RUNNING" and hasattr(node.plugin, "activate")
    ]
    if activations:
        await asyncio.gather(*activations)


async def _reload(kernel, profile_path) -> None:
    """SIGHUP で profile を読み直し、差分だけ適用する。"""

    try:
        plan = resolve(load_profile(Path(profile_path)), discover())
        summary = await kernel.reload(plan)
        activated = 0
        for key in summary["started"]:
            node = kernel.plan.nodes[key]
            if node.state == "RUNNING" and hasattr(node.plugin, "activate"):
                await node.plugin.activate()
                activated += 1
        print(
            f"jllm: reloaded ({len(summary['started'])} started, "
            f"{len(summary['stopped'])} stopped, {activated} activated)",
            file=sys.stderr,
        )
    except Exception as exc:
        print(
            f"jllm: reload failed ({type(exc).__name__}); keeping the running plugins",
            file=sys.stderr,
        )


async def _run(plan, profile_path=None) -> int:
    kernel = Kernel(plan)
    loop = asyncio.get_running_loop()
    stopped = asyncio.Event()
    reloaded = asyncio.Event()
    signals = (signal.SIGINT, signal.SIGTERM)
    for sig in signals:
        loop.add_signal_handler(sig, stopped.set)
    can_reload = profile_path is not None and hasattr(signal, "SIGHUP")
    if can_reload:
        loop.add_signal_handler(signal.SIGHUP, reloaded.set)
    startup = asyncio.create_task(kernel.start())
    activation = None
    reload_wait = None
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

            def activation_done(task):
                if task.cancelled() or task.exception() is None:
                    return
                logging.getLogger("jllm").error(
                    "plugin activation failed", exc_info=task.exception()
                )
                stopped.set()

            activation.add_done_callback(activation_done)
            if not can_reload:
                # 直接呼び出し（テスト・単発起動）は従来どおりの待ち方にする。
                done, _ = await asyncio.wait(
                    (activation, shutdown),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if shutdown in done and not activation.done():
                    activation.cancel()
                    await asyncio.gather(activation, return_exceptions=True)
                else:
                    await activation
            else:
                # Discord の接続のように activate が戻らないプラグインがあるため、
                # 完了を待たずに SIGHUP を待つ。
                while not shutdown.done():
                    reload_wait = asyncio.create_task(reloaded.wait())
                    done, _ = await asyncio.wait(
                        (shutdown, reload_wait),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if shutdown in done:
                        break
                    reloaded.clear()
                    reload_wait.cancel()
                    await asyncio.gather(reload_wait, return_exceptions=True)
                    await _reload(kernel, profile_path)
    except Exception:
        print("jllm: plugin startup failed", file=sys.stderr)
        exit_code = 1
    finally:
        for task in (startup, activation, shutdown, reload_wait):
            if task is not None:
                task.cancel()
        await asyncio.gather(
            *(task for task in (startup, activation, shutdown, reload_wait) if task is not None),
            return_exceptions=True,
        )
        try:
            await kernel.stop()
        except Exception:
            print("jllm: plugin shutdown failed", file=sys.stderr)
            exit_code = 1
        for sig in signals:
            loop.remove_signal_handler(sig)
        if can_reload:
            loop.remove_signal_handler(signal.SIGHUP)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
