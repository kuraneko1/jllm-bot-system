"""Discovery, profile validation, dependency resolution and lifecycle."""

from __future__ import annotations

import asyncio
import importlib
import importlib.metadata
import importlib.resources
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jsonschema
import yaml

from .api import PluginContext, ResourceScope, _await_cleanup


class ProfileError(Exception):
    """The complete profile must be rejected before plugin startup."""


@dataclass(frozen=True)
class Capability:
    capability: str
    version: int
    cardinality: str = "one"


@dataclass(frozen=True)
class Manifest:
    id: str
    version: str
    api: str
    entry: str
    provides: tuple[Capability, ...]
    requires: tuple[Capability, ...]
    permissions: tuple[str, ...] = ()
    config_schema: dict[str, Any] = field(default_factory=dict)
    multi_instance: bool = False
    package: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any], package: str = "") -> Manifest:
        try:

            def caps(key: str, default: str) -> tuple[Capability, ...]:
                result = []
                for item in raw.get(key, []):
                    name, version = item["capability"], item["version"]
                    cardinality = item.get("cardinality", default)
                    if (
                        not isinstance(name, str)
                        or not name
                        or type(version) is not int
                        or version < 1
                        or cardinality not in ("one", "many", "optional_one")
                    ):
                        raise ValueError("invalid capability")
                    result.append(Capability(name, version, cardinality))
                return tuple(result)

            ident, version, api, entry = (raw[k] for k in ("id", "version", "api", "entry"))
            if (
                not all(isinstance(x, str) and x for x in (ident, version, api, entry))
                or "/" in ident
                or ":" not in entry
            ):
                raise ValueError("invalid manifest identity")
            if not re.fullmatch(r"\d+\.\d+\.\d+", version):
                raise ValueError("invalid plugin version")
            if not _api_compatible(api):
                raise ValueError("incompatible API version")
            schema = raw.get("config_schema", {})
            if isinstance(schema, str):
                if not package or "/" in schema or "\\" in schema:
                    raise ValueError("invalid schema resource")
                schema = json.loads(
                    importlib.resources.files(package).joinpath(schema).read_text(encoding="utf-8")
                )
            if not isinstance(schema, dict):
                raise ValueError("invalid config schema")
            jsonschema.validators.validator_for(schema).check_schema(schema)
            provides = caps("provides", "one")
            requires = caps("requires", "one") + caps("optional", "optional_one")
            if any(c.cardinality == "optional_one" for c in caps("requires", "one")) or any(
                c.cardinality != "optional_one" for c in caps("optional", "optional_one")
            ):
                raise ValueError("invalid required/optional cardinality")
            if len({c.capability for c in provides}) != len(provides) or len(
                {c.capability for c in requires}
            ) != len(requires):
                raise ValueError("duplicate capability")
            multi = raw.get("multi_instance", False)
            permissions = raw.get("permissions", [])
            if (
                type(multi) is not bool
                or not isinstance(permissions, list)
                or not all(isinstance(x, str) for x in permissions)
            ):
                raise ValueError("invalid manifest fields")
            return cls(
                ident,
                version,
                api,
                entry,
                provides,
                requires,
                tuple(permissions),
                schema,
                multi,
                package,
            )
        except (
            AttributeError,
            KeyError,
            TypeError,
            ValueError,
            OSError,
            json.JSONDecodeError,
            jsonschema.SchemaError,
        ) as exc:
            raise ProfileError("invalid plugin manifest") from exc


def _api_compatible(spec: str) -> bool:
    # API version is 1.0; only explicit comparison clauses are supported.
    for clause in spec.split(","):
        m = re.fullmatch(r"\s*(>=|<=|==|>|<)\s*(\d+)(?:\.(\d+))?(?:\.(\d+))?\s*", clause)
        if not m:
            return False
        op = m[1]
        v = tuple(int(x or 0) for x in m.group(2, 3, 4))
        current = (1, 0, 0)
        if not {
            ">=": current >= v,
            "<=": current <= v,
            "==": current == v,
            ">": current > v,
            "<": current < v,
        }[op]:
            return False
    return True


def discover() -> dict[str, Manifest]:
    found = {}
    for ep in importlib.metadata.entry_points(group="jllm.plugins"):
        try:
            package, resource = ep.value.split(":", 1)
            if not package or not resource or "/" in resource or "\\" in resource:
                raise ValueError("invalid resource")
            raw = json.loads(
                importlib.resources.files(package).joinpath(resource).read_text(encoding="utf-8")
            )
            manifest = Manifest.from_dict(raw, package)
        except (ValueError, ModuleNotFoundError, OSError, json.JSONDecodeError) as exc:
            raise ProfileError(f"invalid manifest resource for {ep.name}") from exc
        if manifest.id in found:
            raise ProfileError(f"duplicate plugin id: {manifest.id}")
        found[manifest.id] = manifest
    return found


@dataclass(frozen=True)
class Profile:
    plugins: dict[str, Any]
    bindings: dict[str, Any] = field(default_factory=dict)


def load_profile(path: str | Path) -> Profile:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ProfileError("cannot read profile") from exc
    if (
        not isinstance(raw, dict)
        or not isinstance(raw.get("plugins"), dict)
        or not isinstance(raw.get("bindings", {}), dict)
    ):
        raise ProfileError("invalid profile structure")
    return Profile(raw["plugins"], raw.get("bindings", {}))


@dataclass
class Node:
    id: str
    manifest: Manifest
    config: dict[str, Any]
    critical: bool
    bindings: dict[str, list[str]] = field(default_factory=dict)
    state: str = "PENDING"
    reason: str = ""
    scope: ResourceScope | None = None
    plugin: Any = None
    services: dict[str, Any] = field(default_factory=dict)


@dataclass
class Plan:
    nodes: dict[str, Node]
    order: list[str]


def resolve(profile: Profile, discovered: dict[str, Manifest]) -> Plan:
    nodes: dict[str, Node] = {}
    for ident, options in profile.plugins.items():
        if (
            not isinstance(ident, str)
            or not isinstance(options, dict)
            or type(options.get("enabled", True)) is not bool
        ):
            raise ProfileError("invalid plugin selection")
        if not options.get("enabled", True):
            continue
        if ident not in discovered:
            raise ProfileError(f"unknown plugin: {ident}")
        manifest = discovered[ident]
        critical = options.get("critical", False)
        if type(critical) is not bool:
            raise ProfileError(f"invalid critical flag: {ident}")
        instances = options.get("instances", {"main": options.get("config", {})})
        if "instances" in options and "config" in options:
            raise ProfileError(f"ambiguous instance configuration: {ident}")
        if (
            not isinstance(instances, dict)
            or not instances
            or (len(instances) > 1 and not manifest.multi_instance)
        ):
            raise ProfileError(f"invalid instances: {ident}")
        for name, config in instances.items():
            if not isinstance(name, str) or not name or "/" in name or not isinstance(config, dict):
                raise ProfileError(f"invalid instance: {ident}")
            errors = list(
                jsonschema.Draft202012Validator(manifest.config_schema).iter_errors(config)
            )
            if errors:
                path = ".".join(str(x) for x in errors[0].absolute_path)
                raise ProfileError(f"invalid config: {ident}/{name} field {path or '<root>'}")
            key = f"{ident}/{name}"
            nodes[key] = Node(key, manifest, config, critical)
    for key, value in profile.bindings.items():
        if (
            not isinstance(key, str)
            or not isinstance(value, dict)
            or not any(key in (n.id, n.manifest.id) for n in nodes.values())
        ):
            raise ProfileError("unknown binding consumer")
        relevant = [n for n in nodes.values() if key in (n.id, n.manifest.id)]
        if any(
            cap not in {r.capability for r in n.manifest.requires}
            for cap in value
            for n in relevant
        ):
            raise ProfileError(f"unknown binding capability: {key}")
    for node in nodes.values():
        bound = dict(profile.bindings.get(node.manifest.id, {}))
        bound.update(profile.bindings.get(node.id, {}))
        for req in node.manifest.requires:
            candidates = [
                n.id
                for n in nodes.values()
                if any(
                    c.capability == req.capability and c.version == req.version
                    for c in n.manifest.provides
                )
            ]
            if (
                req.capability not in bound
                and not candidates
                and any(
                    c.capability == req.capability
                    for provider in nodes.values()
                    for c in provider.manifest.provides
                )
            ):
                raise ProfileError(f"incompatible capability version: {node.id}/{req.capability}")
            if req.capability in bound:
                selection = bound[req.capability]
                if req.cardinality == "many":
                    if (
                        not isinstance(selection, list)
                        or not all(isinstance(x, str) for x in selection)
                        or len(set(selection)) != len(selection)
                    ):
                        raise ProfileError(f"invalid many binding: {node.id}/{req.capability}")
                else:
                    if not isinstance(selection, str):
                        raise ProfileError(f"invalid one binding: {node.id}/{req.capability}")
                    selection = [selection]
                for target in selection:
                    if target not in nodes or target not in candidates:
                        raise ProfileError(
                            f"unknown or incompatible binding: {node.id}/{req.capability}"
                        )
            elif req.cardinality == "many":
                selection = []
            else:
                if len(candidates) > 1:
                    raise ProfileError(f"ambiguous binding: {node.id}/{req.capability}")
                selection = candidates
            node.bindings[req.capability] = selection
            if not selection and req.cardinality != "optional_one":
                node.state = "BLOCKED"
                node.reason = f"missing provider: {req.capability}"
    order: list[str] = []
    visited: set[str] = set()
    visiting: set[str] = set()

    def visit(key: str) -> None:
        if key in visiting:
            raise ProfileError("dependency cycle")
        if key in visited:
            return
        visiting.add(key)
        for targets in nodes[key].bindings.values():
            for target in targets:
                visit(target)
        visiting.remove(key)
        visited.add(key)
        order.append(key)

    for key in nodes:
        visit(key)
    for key in order:
        node = nodes[key]
        for req in node.manifest.requires:
            if req.cardinality != "optional_one" and any(
                nodes[t].state == "BLOCKED" for t in node.bindings[req.capability]
            ):
                node.state, node.reason = "BLOCKED", f"blocked dependency: {req.capability}"
        if node.critical and node.state == "BLOCKED":
            raise ProfileError(f"critical plugin blocked: {node.id}")
    return Plan(nodes, order)


class Kernel:
    def __init__(self, plan: Plan):
        self.plan = plan
        self._started: list[str] = []
        self._stop_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._stop_task is not None:
            if not self._stop_task.done():
                raise RuntimeError("plugin shutdown is in progress")
            self._stop_task = None
        try:
            for key in self.plan.order:
                if self.plan.nodes[key].state == "BLOCKED":
                    continue
                await self._start_one(self.plan, key)
        except BaseException:
            await self.stop()
            raise

    async def _start_one(self, plan: Plan, key: str) -> bool:
        """1つのノードを用意して起動する。起動したら True。"""

        node = plan.nodes[key]
        missing = [
            r
            for r in node.manifest.requires
            if r.cardinality != "optional_one"
            and any(plan.nodes[t].state != "RUNNING" for t in node.bindings[r.capability])
        ]
        if missing:
            node.state, node.reason = "BLOCKED", f"failed dependency: {missing[0].capability}"
            if node.critical:
                raise RuntimeError(f"critical plugin blocked: {key}")
            return False
        node.scope = ResourceScope(key)
        try:
            module, attr = node.manifest.entry.split(":", 1)
            factory = getattr(importlib.import_module(module), attr)
            node.plugin = factory()
            deps = {}
            for req in node.manifest.requires:
                targets = [
                    plan.nodes[t]
                    for t in node.bindings[req.capability]
                    if plan.nodes[t].state == "RUNNING"
                ]
                values = [target.services[req.capability] for target in targets]
                deps[req.capability] = (
                    values if req.cardinality == "many" else (values[0] if values else None)
                )
            node.state = "SETUP"
            dependency_ids = {
                req.capability: list(node.bindings[req.capability])
                for req in node.manifest.requires
            }
            services = await node.plugin.setup(
                PluginContext(
                    node.config,
                    deps,
                    node.scope,
                    logging.getLogger("jllm.plugin." + key),
                    dependency_ids,
                )
            )
            if not isinstance(services, dict) or set(services) != {
                c.capability for c in node.manifest.provides
            }:
                raise ValueError("provided services do not match manifest")
            node.services = services
            node.state = "STARTING"
            await node.plugin.start()
            node.state = "RUNNING"
            self._started.append(key)
            return True
        except BaseException as exc:
            node.state, node.reason = "FAILED", "plugin initialization failed"
            try:
                await node.scope.close()
            except Exception:
                logging.getLogger(__name__).error("scope cleanup failed: %s", key)
            node.scope = None
            if not isinstance(exc, Exception):
                raise
            if node.critical:
                raise RuntimeError(f"critical plugin failed: {key}") from None
            return False

    async def _stop_one(self, node: Node) -> None:
        node.state = "STOPPING"
        try:
            await node.plugin.stop()
        except (Exception, asyncio.CancelledError):
            logging.getLogger(__name__).error("plugin stop failed: %s", node.id)
        try:
            await node.scope.close()
        except (Exception, asyncio.CancelledError):
            logging.getLogger(__name__).error("scope cleanup failed: %s", node.id)
        node.state = "STOPPED"

    @staticmethod
    def _signature(node: Node) -> str:
        return json.dumps(
            [node.manifest.version, node.config, node.bindings, node.critical],
            sort_keys=True,
            default=str,
        )

    @staticmethod
    def _dependents(plan: Plan, keys: set[str]) -> set[str]:
        """keys を（推移的に）参照しているノードを集める。"""

        found: set[str] = set()

        while True:
            grew = False
            for node in plan.nodes.values():
                if node.id in found or node.id in keys:
                    continue
                if any(
                    target in keys or target in found
                    for targets in node.bindings.values()
                    for target in targets
                ):
                    found.add(node.id)
                    grew = True
            if not grew:
                return found

    async def reload(self, new_plan: Plan) -> dict[str, list[str]]:
        """稼働中の plan に差分だけ適用する（設定変更・着脱）。

        - 追加・変更・削除されたノードと、その依存側だけを止めて入れ直す。
        - 失敗したら元の plan を組み直して戻す。
        """

        if self._stop_task is not None and not self._stop_task.done():
            raise RuntimeError("plugin shutdown is in progress")
        old = self.plan
        old_running = {key for key in self._started if old.nodes[key].state == "RUNNING"}
        old_sig = {key: self._signature(node) for key, node in old.nodes.items()}
        new_sig = {key: self._signature(node) for key, node in new_plan.nodes.items()}

        changed = {k for k in old_sig.keys() & new_sig.keys() if old_sig[k] != new_sig[k]}
        removed = old_sig.keys() - new_sig.keys()
        added = new_sig.keys() - old_sig.keys()
        touched = changed | removed
        restart = (
            changed
            | added
            | self._dependents(old, touched)
            | self._dependents(new_plan, touched)
        ) & new_sig.keys()

        # 変わらないノードは、動いている実体をそのまま引き継ぐ。
        for key in new_sig.keys() - restart:
            if old.nodes.get(key) is not None and old.nodes[key].state == "RUNNING":
                new_plan.nodes[key] = old.nodes[key]

        stopped: list[str] = []
        for key in reversed(old.order):
            if key not in restart and key not in removed:
                continue
            node = old.nodes[key]
            if node.state == "RUNNING" or node.scope is not None:
                await self._stop_one(node)
                stopped.append(key)
            self._started = [item for item in self._started if item != key]

        self.plan = new_plan
        started: list[str] = []
        try:
            for key in new_plan.order:
                if key not in restart:
                    continue
                if new_plan.nodes[key].state == "BLOCKED":
                    continue
                if await self._start_one(new_plan, key):
                    started.append(key)
        except BaseException:
            # 新しく起動した分を戻し、元の plan を組み直す。
            for key in reversed(started):
                node = new_plan.nodes[key]
                if node.state in ("RUNNING", "STARTING", "SETUP"):
                    await self._stop_one(node)
                self._started = [item for item in self._started if item != key]
            self.plan = old
            for key in old.order:
                if key in old_running and old.nodes[key].state != "RUNNING":
                    try:
                        await self._start_one(old, key)
                    except Exception:
                        logging.getLogger(__name__).error("rollback failed: %s", key)
            raise

        return {
            "started": started,
            "stopped": stopped,
            "unchanged": sorted(new_sig.keys() - restart),
        }

    async def stop(self) -> None:
        if self._stop_task is None:
            self._stop_task = asyncio.create_task(self._stop())
        await _await_cleanup(self._stop_task)

    async def _stop(self) -> None:
        errors = []
        while self._started:
            node = self.plan.nodes[self._started.pop()]
            node.state = "STOPPING"
            try:
                await node.plugin.stop()
            except Exception, asyncio.CancelledError:
                errors.append(RuntimeError(f"plugin stop failed: {node.id}"))
            try:
                await node.scope.close()
            except Exception, asyncio.CancelledError:
                errors.append(RuntimeError(f"scope cleanup failed: {node.id}"))
            node.state = "STOPPED"
        if errors:
            raise ExceptionGroup("plugin shutdown failed", errors)
