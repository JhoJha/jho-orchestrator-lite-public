#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# This runner is intentionally pinned to the exact protoAgent source checkout
# inspected during Phase 1/2. It uses the upstream AcpClient and permission
# resolver, adding only model+effort configuration after session/new.
PROTO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROTO_ROOT))

# M-SAN-001: canonical shared redaction policy; jho_safe ships beside this
# runner and is required (scripts/validate_repo.py enforces its presence).
_RUNNER_DIR = Path(__file__).resolve().parent
if str(_RUNNER_DIR) not in sys.path:
    sys.path.insert(0, str(_RUNNER_DIR))
import jho_safe

PINNED_PROTO_VERSION = "0.163.0"
PINNED_PROTO_HEAD = "8134031fa1f0f51ec557595189eb44d1eded5da8"

REGISTRY_PATH = PROTO_ROOT / "local_jho" / "config" / "candidate_registry_v1.json"
ROUTING_PATH = PROTO_ROOT / "local_jho" / "config" / "routing_policy_v1.json"
TELEMETRY_PATH = PROTO_ROOT / "local_jho" / "telemetry" / "runs.jsonl"
DEFAULT_PRODUCT = Path.home() / "repos" / "client-product"

from plugins.coding_agent import _make_permission  # pinned private seam, guarded by HEAD check
from plugins.coding_agent.acp_client import AcpClient, AcpError, TappedResult


class PolicyError(RuntimeError):
    pass


class CapabilityError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_sync(args: list[str], cwd: Path | None = None, timeout: int = 20) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, text=True, capture_output=True, timeout=timeout)


def git_head(path: Path) -> str | None:
    try:
        p = run_sync(["git", "rev-parse", "HEAD"], cwd=path)
        return p.stdout.strip() if p.returncode == 0 else None
    except Exception:
        return None


def git_status(path: Path) -> str | None:
    try:
        p = run_sync(["git", "status", "--porcelain=v1"], cwd=path)
        return p.stdout if p.returncode == 0 else None
    except Exception:
        return None


def proto_version() -> str:
    try:
        data = tomllib.loads((PROTO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        return str(data["project"]["version"])
    except Exception:
        return "unknown"


def ensure_pinned_upstream() -> None:
    version = proto_version()
    head = git_head(PROTO_ROOT)
    if version != PINNED_PROTO_VERSION or head != PINNED_PROTO_HEAD:
        raise PolicyError(
            "protoAgent upstream drift detected. "
            f"expected version={PINNED_PROTO_VERSION} head={PINNED_PROTO_HEAD}; "
            f"found version={version} head={head}. "
            "Refuse to use pinned private ACP seams until re-inspected."
        )


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_registry() -> dict[str, Any]:
    if not REGISTRY_PATH.exists():
        raise PolicyError(f"Candidate registry missing: {REGISTRY_PATH}")
    return load_json(REGISTRY_PATH)


def parse_candidate(value: str) -> tuple[str, str, str]:
    # Syntax: pool:model@effort
    # Example: opencode-go:deepseek-v4.1-flash@high
    if ":" not in value or "@" not in value:
        raise PolicyError(
            "candidate syntax must be pool:model@effort; "
            "e.g. opencode-go:deepseek-v4.1-flash@high"
        )
    pool, rest = value.split(":", 1)
    model, effort = rest.rsplit("@", 1)
    if not pool or not model or not effort:
        raise PolicyError("candidate contains an empty pool/model/effort")
    return pool, model, effort


def pool_key(pool: str) -> str:
    mapping = {
        "codex": "codex",
        "agy": "agy",
        "opencode-free": "opencode_free",
        "opencode-go": "opencode_go",
        "nvidia": "nvidia_background_long",
    }
    try:
        return mapping[pool]
    except KeyError:
        raise PolicyError(f"unknown candidate pool: {pool}") from None


def find_registry_candidate(reg: dict[str, Any], pool: str, model: str, effort: str) -> dict[str, Any]:
    key = pool_key(pool)
    entries = reg["pools"].get(key) or []
    row = next((x for x in entries if str(x.get("model")) == model), None)
    if not row:
        raise PolicyError(f"model not present in active registry pool {pool}: {model}")
    efforts = [str(x) for x in (row.get("efforts") or [])]
    if effort not in efforts:
        raise PolicyError(
            f"effort '{effort}' is not registered for {pool}:{model}; "
            f"allowed={efforts}"
        )
    return row


def enforce_hard_rules(pool: str, model: str, execution_class: str) -> None:
    if pool == "codex" and model == "gpt-6-astra":
        raise PolicyError("RULE-001: Astra is permanently denied.")

    if pool.startswith("opencode") and (
        model.lower().startswith("gpt-") or "/gpt-" in model.lower()
    ):
        raise PolicyError("RULE-002: GPT is not routed through OpenCode.")

    if pool == "nvidia" and execution_class != "background_long":
        raise PolicyError("RULE-003: NVIDIA is background_long only.")

    if pool != "nvidia" and execution_class == "background_long":
        # Allowed: background class can use non-NVIDIA too.
        pass


def adapter_for(pool: str, model: str) -> tuple[str, list[str], str, str]:
    if pool == "codex":
        return "codex-acp", [], model, "codex"
    if pool == "opencode-go":
        return "opencode", ["acp"], f"opencode-go/{model}", "opencode-go"
    if pool == "opencode-free":
        return "opencode", ["acp"], f"opencode/{model}", "opencode-free"
    if pool == "nvidia":
        return "opencode", ["acp"], f"nvidia/{model}", "nvidia"
    if pool == "agy":
        return "agy", [], model, "agy"
    raise PolicyError(f"unsupported pool: {pool}")


def config_options(result: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(result, dict):
        return []
    raw = result.get("configOptions")
    if raw is None:
        raw = result.get("sessionConfigOptions")
    return raw if isinstance(raw, list) else []


def option_values(opt: dict[str, Any] | None) -> list[str]:
    if not opt:
        return []
    vals: list[str] = []
    for item in opt.get("options") or []:
        if isinstance(item, dict) and item.get("value") is not None:
            vals.append(str(item["value"]))
    return vals


def find_option(
    opts: list[dict[str, Any]],
    *,
    category: str | None = None,
    ids: tuple[str, ...] = (),
) -> dict[str, Any] | None:
    if category:
        for opt in opts:
            if str(opt.get("category") or "") == category:
                return opt
    for ident in ids:
        for opt in opts:
            if str(opt.get("id") or "") == ident:
                return opt
    return None


class LiveProgress:
    """Small terminal observability layer for interactive Jho Lite runs.

    It never prints chain-of-thought. It only exposes lifecycle, counts, tool
    metadata, elapsed time, ACP child liveness, and idle duration.
    """

    def __init__(
        self,
        *,
        mode: str = "normal",
        heartbeat_s: float = 10.0,
        stall_warn_s: float = 45.0,
    ):
        self.mode = mode
        self.heartbeat_s = max(2.0, float(heartbeat_s))
        self.stall_warn_s = max(self.heartbeat_s, float(stall_warn_s))
        self.started = time.monotonic()
        self.last_activity = self.started
        self.phase_name = "starting"
        self.last_event = "initializing"
        self._stop = asyncio.Event()
        self._last_tool_line = None
        self._stall_level = 0

    def _elapsed(self) -> str:
        sec = int(time.monotonic() - self.started)
        return f"{sec // 60:02d}:{sec % 60:02d}"

    def _print(self, message: str) -> None:
        if self.mode != "quiet":
            print(f"[{self._elapsed()}] {message}", flush=True)

    def phase(self, name: str, detail: str | None = None) -> None:
        self.phase_name = name
        self.last_activity = time.monotonic()
        self.last_event = detail or name
        msg = f"▶ {name}"
        if detail:
            msg += f" — {detail}"
        self._print(msg)

    def activity(self, kind: str, detail: str | None = None) -> None:
        self.last_activity = time.monotonic()
        self.last_event = detail or kind
        self._stall_level = 0
        if self.mode == "verbose":
            msg = f"· {kind}"
            if detail:
                msg += f" — {detail}"
            self._print(msg)

    def tool(self, number: int, summary: dict[str, Any]) -> None:
        self.last_activity = time.monotonic()
        self._stall_level = 0
        title = (
            summary.get("title")
            or summary.get("kind")
            or summary.get("type")
            or "tool"
        )
        status = summary.get("status")
        detail = str(title)[:160]
        if status:
            detail += f" [{status}]"
        self.last_event = f"tool #{number}: {detail}"

        # Normal mode prints a useful tool breadcrumb while avoiding exact duplicates.
        line_key = (detail, status)
        if self.mode in {"normal", "verbose"} and line_key != self._last_tool_line:
            self._print(f"⚙ TOOL #{number} — {detail}")
            self._last_tool_line = line_key

    async def heartbeat(
        self,
        *,
        counters: dict[str, int],
        client_getter,
    ) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.heartbeat_s)
                break
            except asyncio.TimeoutError:
                pass

            client = client_getter()
            proc = getattr(client, "_proc", None) if client is not None else None
            if proc is None:
                proc_state = "not-started"
                pid = None
            elif proc.returncode is None:
                proc_state = "alive"
                pid = proc.pid
            else:
                proc_state = f"exited({proc.returncode})"
                pid = proc.pid

            idle = time.monotonic() - self.last_activity
            base = (
                f"♥ WORKING phase={self.phase_name} "
                f"proc={proc_state}"
                + (f" pid={pid}" if pid else "")
                + f" tools={counters['tool_events']}"
                f" thoughts={counters['thought_chunks']}"
                f" text={counters['text_chunks']}"
                f" idle={int(idle)}s"
            )
            self._print(base)

            if idle >= self.stall_warn_s:
                level = int(idle // self.stall_warn_s)
                if level > self._stall_level:
                    self._stall_level = level
                    if proc_state == "alive":
                        self._print(
                            "⚠ SIN ACTIVIDAD ACP visible "
                            f"durante {int(idle)}s; el proceso sigue vivo. "
                            "Puede estar razonando/esperando I/O o estar atascado; no se cancela automáticamente."
                        )
                    else:
                        self._print(
                            "⚠ El agente no muestra actividad y su proceso ya no figura activo."
                        )

    def stop(self) -> None:
        self._stop.set()


AGY_SETTINGS = Path.home() / ".gemini" / "antigravity-cli" / "settings.json"

# AGY headless supports explicit low/medium/high. Models whose registry variant
# is "thinking" expose that mode as a model-native configuration; no invalid
# --effort=thinking flag is sent.
_AGY_FLAG_EFFORTS = {"low", "medium", "high"}

_PERMISSION_PATTERNS = (
    re.compile(r"(?i)\bpermission\b.*\b(denied|blocked|approval|allow)\b"),
    re.compile(r"(?i)\bsoft[- ]?denied\b"),
    re.compile(r"(?i)\brequires? (?:your )?approval\b"),
    re.compile(r"(?i)\bnot permitted\b"),
    re.compile(r"(?i)\bhow to allow\b"),
)


def agy_settings_summary() -> dict[str, Any]:
    out: dict[str, Any] = {
        "path": str(AGY_SETTINGS),
        "exists": AGY_SETTINGS.exists(),
        "toolPermission": None,
        "enableTerminalSandbox": None,
        "artifactReviewPolicy": None,
    }
    if not AGY_SETTINGS.exists():
        return out
    try:
        data = json.loads(AGY_SETTINGS.read_text(encoding="utf-8"))
        out["toolPermission"] = data.get("toolPermission")
        out["enableTerminalSandbox"] = data.get("enableTerminalSandbox")
        out["artifactReviewPolicy"] = data.get("artifactReviewPolicy")
        perms = data.get("permissions") or {}
        out["allow_count"] = len(perms.get("allow") or [])
        out["deny_count"] = len(perms.get("deny") or [])
        out["ask_count"] = len(perms.get("ask") or [])
        out["ask_command_all"] = "command(*)" in (perms.get("ask") or [])
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out



_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _agy_catalog_slugs() -> list[str]:
    """Read AGY's authenticated model catalog without running inference."""
    exe = shutil.which("agy")
    if not exe:
        return []
    try:
        p = subprocess.run(
            [exe, "models"],
            text=True,
            capture_output=True,
            timeout=30,
        )
    except Exception:
        return []
    raw = _ANSI_RE.sub("", (p.stdout or "") + "\n" + (p.stderr or ""))
    slugs: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith(("Available", "Model", "─", "-", "=")):
            continue
        token = line.split()[0].strip()
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", token):
            slugs.append(token)
    # Preserve catalog order while removing duplicates.
    return list(dict.fromkeys(slugs))


def resolve_agy_wire_model(model: str, effort: str) -> tuple[str, list[str], str]:
    """Resolve registry model+effort to the CLI slug actually exposed by AGY.

    AGY commonly encodes Gemini/GPT-OSS effort in the model slug
    (e.g. gemini-3.8-flash-high), while Claude Thinking can be model-native.
    """
    slugs = _agy_catalog_slugs()

    candidates: list[tuple[str, str]] = []
    if effort in {"low", "medium", "high"}:
        candidates.append((f"{model}-{effort}", "effort_encoded_in_slug"))
    candidates.append((model, "exact_registry_model"))

    for candidate, source in candidates:
        if candidate in slugs:
            # If effort is already encoded in the slug, don't redundantly pass --effort.
            if source == "effort_encoded_in_slug":
                return candidate, [], source
            effort_args, effort_source = _agy_effort_args(model, effort)
            return candidate, effort_args, effort_source

    # Catalog parsing may change between AGY versions. Fail softly to the documented
    # base-model + --effort contract for low/medium/high and let AGY reject loudly if
    # that version does not accept the base slug.
    effort_args, effort_source = _agy_effort_args(model, effort)
    return model, effort_args, f"catalog_fallback:{effort_source}"



def ensure_agy_workdir_read_permission(workdir: Path) -> dict[str, Any]:
    """Ensure headless AGY can read the exact active workspace recursively.

    Headless cannot prompt. Even though AGY documents workspace reads as normally
    auto-allowed, observed AGY 1.2.1 may still soft-deny read_file. We therefore
    add an explicit scoped allow rule for the exact run workdir.

    This is not a global read_file(*) grant.
    """
    workdir = workdir.resolve()
    target = f"read_file({workdir})"

    if not AGY_SETTINGS.exists():
        raise PolicyError(f"AGY settings file missing: {AGY_SETTINGS}")

    try:
        data = json.loads(AGY_SETTINGS.read_text(encoding="utf-8"))
    except Exception as exc:
        raise PolicyError(f"cannot parse AGY settings: {type(exc).__name__}: {exc}") from exc

    if not isinstance(data, dict):
        raise PolicyError("AGY settings root is not a JSON object")

    perms = data.setdefault("permissions", {})
    if not isinstance(perms, dict):
        raise PolicyError("AGY permissions setting is not an object")

    allow = list(perms.get("allow") or [])
    deny = list(perms.get("deny") or [])
    ask = list(perms.get("ask") or [])

    # Deny/Ask outrank Allow. Catch the broad conflicts we can prove safely.
    blocking_deny = [x for x in deny if x in {"read_file(*)", target}]
    blocking_ask = [x for x in ask if x in {"read_file(*)", target}]
    if blocking_deny:
        raise PolicyError(
            "AGY read permission is blocked by deny precedence: "
            + ", ".join(blocking_deny)
        )
    if blocking_ask:
        raise PolicyError(
            "AGY read permission is blocked by ask precedence in headless mode: "
            + ", ".join(blocking_ask)
        )

    changed = False
    if target not in allow:
        allow.append(target)
        perms["allow"] = allow
        tmp = AGY_SETTINGS.with_suffix(".json.jho-tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(AGY_SETTINGS)
        changed = True

    return {
        "target": target,
        "changed": changed,
        "allow_count": len(allow),
    }


def _agy_effort_args(model: str, effort: str) -> tuple[list[str], str]:
    if effort in _AGY_FLAG_EFFORTS:
        return ["--effort", effort], "explicit_flag"
    if effort == "thinking":
        if not model.startswith("claude-"):
            raise PolicyError(
                f"AGY effort 'thinking' is only accepted as a model-native mode for Claude candidates; got {model}"
            )
        return [], "model_native_thinking"
    if effort == "default":
        return [], "provider_default"
    raise PolicyError(f"AGY effort cannot be represented safely by the current CLI adapter: {effort}")


def _agy_timeout_flag(seconds: float) -> str:
    # Headless accepts human durations such as 15m. Keep integer seconds exact.
    return f"{max(1, int(seconds))}s"


def _agy_permission_notice(line: str) -> bool:
    return any(p.search(line) for p in _PERMISSION_PATTERNS)


def _safe_tool_detail(tool_name: str, tool_info: dict[str, Any] | None) -> str:
    if not isinstance(tool_info, dict):
        return tool_name
    params = tool_info.get("parameters")
    if isinstance(params, dict):
        for key in ("CommandLine", "command", "path", "Path", "file_path", "FilePath"):
            val = params.get(key)
            if isinstance(val, str) and val:
                # Terminal breadcrumb only; avoid very long/sensitive argument
                # dumps. Canonical policy (M-SAN-001), no local regex.
                val = redact_text(val)
                return f"{tool_name}: {val[:160]}"
    return tool_name


async def run_agy_headless(
    *,
    task: str,
    model: str,
    effort: str,
    workdir: Path,
    timeout: float,
    progress: LiveProgress,
    counters: dict[str, int],
) -> dict[str, Any]:
    exe = shutil.which("agy")
    if not exe:
        raise PolicyError("agy binary not found on PATH")

    wire_model, effort_args, effort_control = resolve_agy_wire_model(model, effort)
    progress._print(f"agy_wire_model={wire_model}")
    progress._print(f"agy_effort_control={effort_control}")

    # v0.2 is still a read-only orchestration phase. Plan mode + explicit prompt
    # guardrail mirrors the readonly ceiling currently used for ACP workers.
    guarded_task = (
        "JHO LITE READ-ONLY POLICY (mandatory). "
        f"ACTIVE_WORKSPACE={workdir}. "
        "The target repository is EXACTLY the active workspace/current working directory. "
        "Never enumerate, inspect, or read the parent directory, the user's home directory, "
        "or any path outside ACTIVE_WORKSPACE. Start from '.' and use repository-relative "
        "paths whenever possible. Inspect/analyze only. "
        "Do not create, edit, delete, rename, format, commit, push, merge, deploy, "
        "install packages, migrate databases, or intentionally run commands that mutate "
        "the repository. If an outside-workspace action is blocked, do not request approval; "
        "continue using evidence inside ACTIVE_WORKSPACE and report the limitation.\\n\\n"
        + task
    )

    cmd = [
        exe,
        "-p",
        guarded_task,
        "--output-format",
        "stream-json",
        "--model",
        wire_model,
        *effort_args,
        "--print-timeout",
        _agy_timeout_flag(timeout),
        "--sandbox",
        "--mode=plan",
    ]

    progress.phase(
        "agy_start",
        f"model={wire_model} registry_model={model} effort={effort} sandbox=on mode=plan",
    )

    agy_env = os.environ.copy()
    # subprocess(cwd=...) changes the process cwd, but inherited shell metadata such
    # as PWD can remain stale. Keep AGY's workspace signals aligned with --workdir.
    agy_env["PWD"] = str(workdir)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(workdir),
        env=agy_env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )

    proxy = type("_AgyProcProxy", (), {})()
    proxy._proc = proc
    hb = asyncio.create_task(progress.heartbeat(counters=counters, client_getter=lambda: proxy))

    stderr_lines: list[str] = []
    permission_notices: list[str] = []
    events_seen = 0
    seen_tool_steps: set[int] = set()
    result_payload: dict[str, Any] | None = None
    init_payload: dict[str, Any] | None = None
    workspace_mismatch: str | None = None
    last_usage: dict[str, Any] | None = None
    subagents_seen = 0

    async def read_stderr() -> None:
        assert proc.stderr is not None
        while True:
            raw = await proc.stderr.readline()
            if not raw:
                break
            line = raw.decode("utf-8", "replace").rstrip()
            if not line:
                continue
            clean = redact_text(line)
            stderr_lines.append(clean)
            if len(stderr_lines) > 200:
                del stderr_lines[:-200]
            if _agy_permission_notice(clean):
                permission_notices.append(clean[:1000])
                progress._print(f"⛔ AGY POLICY NOTICE — {clean[:180]}")
            elif progress.mode == "verbose":
                progress._print(f"· AGY STDERR — {clean[:180]}")

    stderr_task = asyncio.create_task(read_stderr())

    try:
        assert proc.stdout is not None
        deadline = time.monotonic() + timeout + 30.0
        while True:
            remain = deadline - time.monotonic()
            if remain <= 0:
                raise asyncio.TimeoutError
            try:
                raw = await asyncio.wait_for(proc.stdout.readline(), timeout=min(remain, 30.0))
            except asyncio.TimeoutError:
                # stdout can be quiet while the child is still doing work. The heartbeat
                # already exposes liveness; keep waiting until the real deadline.
                if time.monotonic() < deadline:
                    continue
                raise

            if not raw:
                break
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue

            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                if progress.mode == "verbose":
                    progress._print(f"· AGY NON-JSON — {redact_text(line)[:160]}")
                continue

            events_seen += 1
            event = msg.get("event")

            if event == "init":
                init_payload = msg.get("init") or {}
                permission_mode = init_payload.get("permission_mode")
                selected_model = init_payload.get("model") or model
                init_cwd = init_payload.get("cwd")
                if init_cwd:
                    try:
                        init_cwd_resolved = str(Path(str(init_cwd)).resolve())
                    except Exception:
                        init_cwd_resolved = str(init_cwd)
                    expected_cwd = str(workdir.resolve())
                    if init_cwd_resolved != expected_cwd:
                        workspace_mismatch = (
                            f"AGY init cwd={init_cwd_resolved!r}, expected={expected_cwd!r}"
                        )
                progress.phase(
                    "agy_initialized",
                    (
                        f"model={selected_model} permission_mode={permission_mode or 'unknown'} "
                        f"cwd={init_cwd or 'unknown'}"
                    ),
                )
                continue

            if event == "step_update":
                step = msg.get("step_update") or {}
                step_type = str(step.get("step_type") or "")
                state = str(step.get("state") or "")
                idx = step.get("step_index")
                usage = step.get("usage")
                if isinstance(usage, dict):
                    last_usage = usage

                if step_type == "tool":
                    if isinstance(idx, int) and idx not in seen_tool_steps:
                        seen_tool_steps.add(idx)
                        counters["tool_events"] += 1
                    tool_name = str(step.get("tool_name") or "tool")
                    detail = _safe_tool_detail(tool_name, step.get("tool_info"))
                    progress.tool(max(1, counters["tool_events"]), {
                        "title": detail,
                        "status": state.lower() if state else None,
                    })
                    tool_info = step.get("tool_info")
                    if isinstance(tool_info, dict) and tool_info.get("error"):
                        errtxt = json.dumps(tool_info.get("error"), ensure_ascii=False)
                        if _agy_permission_notice(errtxt):
                            permission_notices.append(redact_text(errtxt)[:1000])
                    continue

                if step_type == "agent_response":
                    delta = step.get("text_delta")
                    if isinstance(delta, str) and delta:
                        counters["text_chunks"] += 1
                        progress.activity("answer_stream")
                    continue

                if step.get("subagent_info"):
                    subagents_seen += 1
                    progress.activity("subagent", f"count={subagents_seen}")
                    continue

                progress.activity(step_type or "step", state or None)
                continue

            if event == "result":
                result_payload = msg.get("result") or {}
                status = result_payload.get("status")
                progress.phase("agy_result", f"status={status}")
                if isinstance(result_payload.get("usage"), dict):
                    last_usage = result_payload["usage"]
                break

        # Let process finish after terminal result/EOF.
        try:
            exit_code = await asyncio.wait_for(proc.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            exit_code = None
    except asyncio.TimeoutError:
        progress.phase("agy_timeout", f"timeout={timeout:.0f}s; killing process group")
        try:
            os.killpg(proc.pid, 15)
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except Exception:
            try:
                os.killpg(proc.pid, 9)
            except Exception:
                proc.kill()
            try:
                await proc.wait()
            except Exception:
                pass
        result_payload = {
            "status": "ERROR",
            "response": "",
            "error": f"Jho Lite timeout after {timeout:.0f}s",
        }
        exit_code = proc.returncode
    finally:
        try:
            await stderr_task
        except Exception:
            pass
        progress.stop()
        try:
            await hb
        except Exception:
            pass

    outside_workspace_notice = False
    workdir_s = str(workdir.resolve())
    for notice in permission_notices:
        for m in re.finditer(r'read_file\\((/[^)]+)\\)', notice):
            try:
                target = str(Path(m.group(1)).resolve())
            except Exception:
                target = m.group(1)
            if target != workdir_s and not target.startswith(workdir_s + os.sep):
                outside_workspace_notice = True
                break
        if outside_workspace_notice:
            break

    rp = result_payload or {}
    provider_status = str(rp.get("status") or "NO_RESULT")
    response = str(rp.get("response") or "")
    error = rp.get("error")
    permission_blocked = bool(permission_notices)

    if workspace_mismatch:
        normalized_status = "workspace_mismatch"
    elif outside_workspace_notice:
        normalized_status = "outside_workspace_blocked"
    elif provider_status == "SUCCESS" and not permission_blocked:
        normalized_status = "completed"
    elif permission_blocked:
        normalized_status = "blocked_by_policy"
    elif provider_status == "WAITING":
        normalized_status = "waiting_input"
    elif provider_status in {"CANCELED", "INTERRUPTED"}:
        normalized_status = "interrupted"
    else:
        normalized_status = "failed"

    configured_model = (init_payload or {}).get("model")
    if configured_model and str(configured_model) not in {wire_model, model}:
        normalized_status = "config_mismatch"
        error = (
            f"AGY init reported model={configured_model!r}, requested_wire={wire_model!r}"
        )

    return {
        "status": normalized_status,
        "provider_status": provider_status,
        "wire_model": wire_model,
        "response": response,
        "error": error,
        "exit_code": exit_code,
        "init": init_payload,
        "usage": last_usage,
        "events_seen": events_seen,
        "subagents_seen": subagents_seen,
        "permission_notices": permission_notices,
        "permission_blocked": permission_blocked,
        "outside_workspace_notice": outside_workspace_notice,
        "workspace_mismatch": workspace_mismatch,
        "stderr_tail": stderr_lines[-50:],
        "applied_config": {
            "model": {
                "value": model,
                "wire_value": wire_model,
                "explicit": True,
                "source": "--model",
            },
            "effort": {
                "value": effort,
                "explicit": effort in _AGY_FLAG_EFFORTS or "effort_encoded_in_slug" in effort_control,
                "source": effort_control,
            },
            "sandbox": True,
            "mode": "plan",
        },
    }


def _powershell_exe() -> str | None:
    found = shutil.which("powershell.exe")
    if found:
        return found
    fallback = Path("/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
    return str(fallback) if fallback.exists() else None


def windows_notification_capability() -> dict[str, Any]:
    """Best-effort probe. Prefer BurntToast, otherwise use a zero-install Windows fallback."""
    ps = _powershell_exe()
    if not ps:
        return {
            "available": False,
            "backend": "windows_notification",
            "reason": "powershell.exe not found through WSL interop",
        }

    try:
        p = subprocess.run(
            [
                ps,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                (
                    "$m=Get-Module -ListAvailable -Name BurntToast | "
                    "Sort-Object Version -Descending | Select-Object -First 1; "
                    "if($m){$m.Version.ToString()}else{exit 3}"
                ),
            ],
            text=True,
            capture_output=True,
            timeout=12,
        )
        if p.returncode == 0:
            return {
                "available": True,
                "backend": "windows_burnttoast",
                "version": (p.stdout or "").strip() or "unknown",
                "fallback": "windows_notifyicon",
            }
    except Exception:
        pass

    try:
        p = subprocess.run(
            [
                ps,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                (
                    "Add-Type -AssemblyName System.Windows.Forms -ErrorAction Stop;"
                    "Add-Type -AssemblyName System.Drawing -ErrorAction Stop;"
                    "exit 0"
                ),
            ],
            text=True,
            capture_output=True,
            timeout=12,
        )
        if p.returncode == 0:
            return {
                "available": True,
                "backend": "windows_notifyicon",
                "version": "built-in",
                "fallback": None,
            }
        reason = (p.stderr or p.stdout or "").strip() or "Windows Forms unavailable"
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"

    return {
        "available": False,
        "backend": "windows_notification",
        "reason": reason,
    }


def notify_windows_best_effort(
    *,
    status: str,
    candidate: str,
    duration_seconds: float,
    integrity_ok: bool,
    enabled: bool = True,
) -> dict[str, Any]:
    """Send a Windows notification from WSL. Failure never fails a run."""
    cap = windows_notification_capability()
    result: dict[str, Any] = {
        "attempted": False,
        "success": False,
        "backend": cap.get("backend"),
        "enabled": bool(enabled),
    }
    if not enabled:
        result["reason"] = "disabled_by_cli"
        return result
    if not cap.get("available"):
        result["reason"] = cap.get("reason") or "no Windows notification backend available"
        return result

    ps = _powershell_exe()
    if not ps:
        result["reason"] = "powershell.exe not found"
        return result

    if status == "completed":
        title = "Jho Lite - tarea completada"
        icon = "Info"
    elif status in {"blocked_by_policy", "outside_workspace_blocked", "waiting_input"}:
        title = "Jho Lite - tarea bloqueada"
        icon = "Warning"
    elif status == "integrity_breach":
        title = "Jho Lite - ALERTA de integridad"
        icon = "Error"
    else:
        title = "Jho Lite - tarea fallida"
        icon = "Error"

    mins = int(duration_seconds // 60)
    secs = int(round(duration_seconds - mins * 60))
    duration_label = f"{mins}m {secs:02d}s" if mins else f"{secs}s"
    integrity_label = "PASS" if integrity_ok else "FAIL"

    # Defense in depth at the notification boundary (M-SAN-001): never trust
    # that the caller already redacted the notification text.
    candidate = redact_text(str(candidate))

    import base64
    import json as _json

    payload = {
        "title": title,
        "line1": candidate,
        "line2": f"{status} | {duration_label} | integrity {integrity_label}",
        "icon": icon,
    }
    encoded = base64.b64encode(
        _json.dumps(payload, ensure_ascii=False).encode("utf-8")
    ).decode("ascii")

    if cap.get("backend") == "windows_burnttoast":
        command = (
            "$ErrorActionPreference='Stop';"
            f"$raw=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded}'));"
            "$j=$raw | ConvertFrom-Json;"
            "Import-Module BurntToast -ErrorAction Stop;"
            "New-BurntToastNotification -Text @($j.title,$j.line1,$j.line2) | Out-Null;"
        )
    else:
        command = (
            "$ErrorActionPreference='Stop';"
            f"$raw=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded}'));"
            "$j=$raw | ConvertFrom-Json;"
            "Add-Type -AssemblyName System.Windows.Forms -ErrorAction Stop;"
            "Add-Type -AssemblyName System.Drawing -ErrorAction Stop;"
            "$n=New-Object System.Windows.Forms.NotifyIcon;"
            "$n.Icon=[System.Drawing.SystemIcons]::Information;"
            "$n.Visible=$true;"
            "$n.BalloonTipTitle=$j.title;"
            "$n.BalloonTipText=($j.line1 + [Environment]::NewLine + $j.line2);"
            "if($j.icon -eq 'Warning'){$n.BalloonTipIcon=[System.Windows.Forms.ToolTipIcon]::Warning}"
            "elseif($j.icon -eq 'Error'){$n.BalloonTipIcon=[System.Windows.Forms.ToolTipIcon]::Error}"
            "else{$n.BalloonTipIcon=[System.Windows.Forms.ToolTipIcon]::Info};"
            "$n.ShowBalloonTip(8000);"
            "Start-Sleep -Seconds 9;"
            "$n.Dispose();"
        )

    result["attempted"] = True
    try:
        p = subprocess.run(
            [
                ps,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                command,
            ],
            text=True,
            capture_output=True,
            timeout=20,
        )
    except Exception as exc:
        result["reason"] = f"{type(exc).__name__}: {exc}"
        return result

    result["returncode"] = p.returncode
    if p.returncode == 0:
        result["success"] = True
        return result

    stderr = (p.stderr or p.stdout or "").strip()
    result["reason"] = redact_text(stderr[-1200:] if stderr else "PowerShell notification failed")
    return result


def notification_test() -> int:
    cap = windows_notification_capability()
    print("Jho Lite Windows notification test")
    print(f"backend: {cap.get('backend')}")
    print(f"available: {cap.get('available')}")
    print(f"fallback: {cap.get('fallback')}")
    if cap.get("version"):
        print(f"BurntToast: {cap.get('version')}")
    if not cap.get("available"):
        print(f"reason: {cap.get('reason')}")
        return 5

    result = notify_windows_best_effort(
        status="completed",
        candidate="notification-test",
        duration_seconds=1.0,
        integrity_ok=True,
        enabled=True,
    )
    print(f"attempted: {result.get('attempted')}")
    print(f"success: {result.get('success')}")
    if result.get("reason"):
        print(f"reason: {result.get('reason')}")
    return 0 if result.get("success") else 5


class ConfiguredAcpClient(AcpClient):
    """Thin compatibility shim over protoAgent v0.163.0.

    The upstream client opens an ACP session but does not preserve/apply
    configOptions. We intercept session/new's result, let upstream finish its normal
    lifecycle, then set model and effort before the first session/prompt.
    """

    def __init__(
        self,
        *args,
        desired_model: str,
        desired_effort: str,
        status_hook=None,
        **kwargs,
    ):
        self.desired_model = desired_model
        self.desired_effort = desired_effort
        self.status_hook = status_hook
        self._jho_session_options: list[dict[str, Any]] = []
        self.applied_config: dict[str, Any] = {}
        super().__init__(*args, **kwargs)

    def _status(self, name: str, detail: str | None = None) -> None:
        if self.status_hook:
            self.status_hook(name, detail)

    async def _request(self, method: str, params: dict, *, timeout: float = 120.0):
        result = await super()._request(method, params, timeout=timeout)
        if method == "session/new":
            self._jho_session_options = config_options(result)
        return result

    async def _open_session(self) -> None:
        self._status("acp_session", "opening fresh ACP session")
        await super()._open_session()
        self._status("acp_session", "session opened; applying exact candidate configuration")
        await self._apply_jho_config()

    async def _apply_jho_config(self) -> None:
        if not self._session_id:
            raise CapabilityError("ACP session opened without session id")

        opts = list(self._jho_session_options)
        model_opt = find_option(opts, category="model", ids=("model",))
        if not model_opt:
            raise CapabilityError("ACP agent did not expose a model config option")

        model_values = option_values(model_opt)
        if self.desired_model not in model_values:
            raise CapabilityError(
                f"requested model not exposed by ACP session: {self.desired_model}; "
                f"available_count={len(model_values)}"
            )

        response = await super()._request(
            "session/set_config_option",
            {
                "sessionId": self._session_id,
                "configId": model_opt["id"],
                "value": self.desired_model,
            },
            timeout=30.0,
        )
        self.applied_config["model"] = {
            "config_id": model_opt["id"],
            "value": self.desired_model,
        }
        self._status("model_configured", self.desired_model)

        updated = config_options(response)
        if updated:
            opts = updated

        if self.desired_effort == "default":
            self.applied_config["effort"] = {
                "config_id": None,
                "value": "default",
                "explicit": False,
            }
            return

        effort_opt = find_option(
            opts,
            category="thought_level",
            ids=("reasoning_effort", "effort"),
        )
        if not effort_opt:
            raise CapabilityError(
                f"ACP agent exposed no reasoning-effort option after selecting {self.desired_model}"
            )

        effort_values = option_values(effort_opt)
        if self.desired_effort not in effort_values:
            raise CapabilityError(
                f"requested effort {self.desired_effort!r} unavailable for "
                f"{self.desired_model}; allowed={effort_values}"
            )

        await super()._request(
            "session/set_config_option",
            {
                "sessionId": self._session_id,
                "configId": effort_opt["id"],
                "value": self.desired_effort,
            },
            timeout=30.0,
        )
        self.applied_config["effort"] = {
            "config_id": effort_opt["id"],
            "value": self.desired_effort,
            "explicit": True,
        }
        self._status("effort_configured", self.desired_effort)


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def redact_text(text: str) -> str:
    # Canonical shared policy (M-SAN-001). Do not mutate model reply semantics
    # beyond credential redaction; no local pattern list may diverge from the
    # shared jho_safe primitive.
    return jho_safe.redact_text(text, max_length=None)


def serialize_tapped(result: TappedResult) -> dict[str, Any]:
    return {
        "usage": result.usage,
        "plan_items": len(result.plan or []),
        "stop_reason": result.stop_reason,
        "dead_end": result.dead_end,
    }


def run_dir(run_id: str) -> Path:
    return Path.home() / "agent-runs" / run_id


async def execute(args: argparse.Namespace) -> int:
    ensure_pinned_upstream()
    reg = load_registry()
    pool, model, effort = parse_candidate(args.candidate)
    find_registry_candidate(reg, pool, model, effort)
    enforce_hard_rules(pool, model, args.execution_class)

    command, cmd_args, wire_model, access_path = adapter_for(pool, model)
    exe = shutil.which(command)
    if not exe:
        raise PolicyError(f"required harness binary not found on PATH: {command}")

    if pool == "agy":
        agy_cfg = agy_settings_summary()
        if agy_cfg.get("toolPermission") != "proceed-in-sandbox":
            raise PolicyError(
                "AGY autonomous-safe policy is not installed: "
                f"toolPermission={agy_cfg.get('toolPermission')!r}. "
                "Run the Phase 4 installer again."
            )
        if not agy_cfg.get("enableTerminalSandbox"):
            raise PolicyError(
                "AGY autonomous-safe policy is not installed: enableTerminalSandbox is not true."
            )

    workdir = Path(args.workdir).expanduser().resolve()
    if not workdir.is_dir():
        raise PolicyError(f"workdir does not exist: {workdir}")

    task = args.task
    if args.task_file:
        task = Path(args.task_file).expanduser().read_text(encoding="utf-8")
    if not task or not task.strip():
        raise PolicyError("task is empty")

    before_head = git_head(workdir)
    before_status = git_status(workdir)
    if before_status not in (None, "") and not args.allow_dirty:
        raise PolicyError(
            "workdir is dirty; refusing read-only benchmark because integrity "
            "comparison would be ambiguous. Commit/stash first or pass --allow-dirty."
        )

    timeout = float(args.timeout)
    if pool == "nvidia" and args.timeout == "900":
        timeout = 14400.0  # 4h background default

    trace_id = uuid.uuid4().hex
    span_id = uuid.uuid4().hex[:16]
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    run_id = f"JHO-LITE-RUN-{os.uname().nodename}-{stamp}-{trace_id[:8]}"
    out = run_dir(run_id)
    out.mkdir(parents=True, exist_ok=True)

    started_at = utc_now()
    started_mono = time.monotonic()

    base_event = {
        "event_version": "1.0",
        "run_id": run_id,
        "task_id": args.task_id or run_id,
        "trace_id": trace_id,
        "span_id": span_id,
        "parent_span_id": None,
        "task_class": args.task_class,
        "risk": args.risk,
        "intrinsic_complexity": args.complexity,
        "historical_difficulty": None,
        "harness": "codex" if pool == "codex" else ("agy" if pool == "agy" else "opencode"),
        "access_path": access_path,
        "provider": access_path,
        "model": model,
        "variant": effort,
        "effort": effort,
        "role": args.role,
        "execution_class": args.execution_class,
    }

    counters = {
        "tool_events": 0,
        "thought_chunks": 0,
        "text_chunks": 0,
    }
    tool_summaries: list[dict[str, Any]] = []

    progress = LiveProgress(
        mode=args.progress,
        heartbeat_s=float(args.heartbeat),
        stall_warn_s=float(args.stall_warn),
    )
    progress.phase("preflight", f"run={run_id}")
    progress._print(f"candidate={args.candidate}")
    progress._print(f"workdir={workdir}")
    progress._print(f"readonly=yes timeout={timeout:.0f}s")

    append_jsonl(TELEMETRY_PATH, {
        **base_event,
        "event_type": "run_started",
        "timestamp": started_at,
        "status": "running",
    })

    # AGY uses its official headless CLI instead of ACP. The logical contract
    # remains the same: explicit model/effort, no interactive permission prompts,
    # progress, timeout, telemetry, and repository-integrity verification.
    if pool == "agy":
        read_scope = ensure_agy_workdir_read_permission(workdir)
        progress.phase(
            "agy_policy",
            "headless + sandbox + proceed-in-sandbox + read-only plan mode",
        )
        progress._print(
            f"agy_read_scope={read_scope['target']} "
            f"changed={str(read_scope['changed']).lower()}"
        )
        try:
            agy_result = await run_agy_headless(
                task=task,
                model=model,
                effort=effort,
                workdir=workdir,
                timeout=timeout,
                progress=progress,
                counters=counters,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            progress.phase("agy_adapter_error", f"{type(exc).__name__}: {exc}")
            agy_result = {
                "status": "failed",
                "provider_status": "ADAPTER_ERROR",
                "wire_model": model,
                "response": "",
                "error": f"{type(exc).__name__}: {exc}",
                "exit_code": None,
                "init": None,
                "usage": None,
                "events_seen": 0,
                "subagents_seen": 0,
                "permission_notices": [],
                "permission_blocked": False,
                "stderr_tail": [],
                "applied_config": {
                    "model": {"value": model, "explicit": False, "source": "adapter_error"},
                    "effort": {"value": effort, "explicit": False, "source": "adapter_error"},
                    "sandbox": True,
                    "mode": "plan",
                },
            }

        duration = time.monotonic() - started_mono
        progress.phase("integrity_check", "verifying HEAD and working-tree status")
        after_head = git_head(workdir)
        after_status = git_status(workdir)
        integrity_ok = (before_head == after_head) and (before_status == after_status)

        status = agy_result["status"]
        if not integrity_ok:
            status = "integrity_breach"

        finished_at = utc_now()
        answer = agy_result.get("response") or ""
        error = agy_result.get("error")
        applied_config = agy_result.get("applied_config") or {}

        notification = notify_windows_best_effort(
            status=status,
            candidate=args.candidate,
            duration_seconds=duration,
            integrity_ok=integrity_ok,
            enabled=not args.no_notify,
        )
        progress._print(
            "notification="
            + ("sent" if notification.get("success") else str(notification.get("reason") or "not-sent"))
        )

        result = {
            "run_id": run_id,
            "trace_id": trace_id,
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_seconds": round(duration, 3),
            "candidate": args.candidate,
            "wire_model": agy_result.get("wire_model") or model,
            "applied_config": applied_config,
            "execution_class": args.execution_class,
            "task_class": args.task_class,
            "risk": args.risk,
            "role": args.role,
            "workdir": str(workdir),
            "permissions": "agy-autonomous-safe-readonly",
            "agy_read_scope": read_scope,
            "status": status,
            "provider_status": agy_result.get("provider_status"),
            "error": error,
            "integrity": {
                "ok": integrity_ok,
                "head_before": before_head,
                "head_after": after_head,
                "status_before": before_status,
                "status_after": after_status,
            },
            "signals": {
                "usage": agy_result.get("usage"),
                "events_seen": agy_result.get("events_seen"),
                "subagents_seen": agy_result.get("subagents_seen"),
                "permission_blocked": agy_result.get("permission_blocked"),
                "permission_notices": agy_result.get("permission_notices"),
                "outside_workspace_notice": agy_result.get("outside_workspace_notice"),
                "workspace_mismatch": agy_result.get("workspace_mismatch"),
                "init_cwd": (agy_result.get("init") or {}).get("cwd"),
                "exit_code": agy_result.get("exit_code"),
            },
            "stream_counters": counters,
            "notification": notification,
            "answer": answer,
        }
        (out / "result.json").write_text(
            json.dumps(result, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        (out / "answer.md").write_text(answer, encoding="utf-8")
        (out / "task.md").write_text(task, encoding="utf-8")

        report = [
            "# Jho Lite Run Report",
            "",
            f"- Run: `{run_id}`",
            f"- Trace: `{trace_id}`",
            f"- Started: `{started_at}`",
            f"- Finished: `{finished_at}`",
            f"- Candidate: `{args.candidate}`",
            f"- Harness: `AGY direct headless`",
            f"- Wire model: `{agy_result.get('wire_model') or model}`",
            f"- Applied config: `{json.dumps(applied_config, ensure_ascii=False)}`",
            f"- Role: `{args.role}`",
            f"- Task class: `{args.task_class}`",
            f"- Risk: `{args.risk}`",
            f"- Execution class: `{args.execution_class}`",
            f"- Duration: `{duration:.1f}s`",
            f"- Status: **{status}**",
            f"- AGY provider status: `{agy_result.get('provider_status')}`",
            f"- Repository integrity: **{'PASS' if integrity_ok else 'FAIL'}**",
            f"- Tool events: `{counters['tool_events']}`",
            f"- Stream events: `{agy_result.get('events_seen')}`",
            f"- Permission blocked: `{agy_result.get('permission_blocked')}`",
            f"- Outside-workspace permission attempt: `{agy_result.get('outside_workspace_notice')}`",
            f"- AGY init cwd: `{(agy_result.get('init') or {}).get('cwd')}`",
            f"- Read scope: `{read_scope.get('target')}`",
            f"- Windows notification: `{'SENT' if notification.get('success') else 'NOT SENT'}`",
            "",
            "## Task",
            "",
            task,
            "",
            "## Agent answer",
            "",
            redact_text(answer) or "_No answer returned._",
        ]
        if agy_result.get("permission_notices"):
            report += ["", "## Permission notices", ""]
            for notice in agy_result["permission_notices"][:20]:
                report.append(f"- `{redact_text(notice)}`")
        if error:
            report += ["", "## Error", "", f"`{redact_text(str(error))}`"]

        report_path = out / "RUN_REPORT.md"
        report_path.write_text("\n".join(report), encoding="utf-8")

        failure_category = None
        if status == "blocked_by_policy":
            failure_category = "PERMISSION_BLOCKED"
        elif status == "outside_workspace_blocked":
            failure_category = "OUTSIDE_WORKSPACE_ATTEMPT"
        elif status == "workspace_mismatch":
            failure_category = "WORKSPACE_MISMATCH"
        elif status == "waiting_input":
            failure_category = "WAITING_FOR_INPUT"
        elif status == "integrity_breach":
            failure_category = "INTEGRITY"
        elif status == "config_mismatch":
            failure_category = "CONFIG_MISMATCH"
        elif status != "completed":
            failure_category = "HARNESS_OR_AGENT"

        append_jsonl(TELEMETRY_PATH, {
            **base_event,
            "event_type": "run_finished",
            "timestamp": finished_at,
            "duration_seconds": round(duration, 3),
            "tests_passed": None,
            "validator_result": "repo_integrity_pass" if integrity_ok else "repo_integrity_fail",
            "review_result": None,
            "review_findings_count": None,
            "attempt": 1,
            "rework_count": 0,
            "first_pass_success": status == "completed" and integrity_ok,
            "final_success": status == "completed" and integrity_ok,
            "failure_category": failure_category,
            "failure_attribution": "agy_permission_policy" if status == "blocked_by_policy" else None,
            "status": status,
            "notes": str(error) if error else None,
        })

        latest = Path.home() / "agent-runs" / "JHO-LITE-RUN-LATEST"
        try:
            if latest.is_symlink() or latest.exists():
                latest.unlink()
            latest.symlink_to(out)
        except Exception:
            pass

        export_md = Path.home() / "wsl-files" / "Exports" / "Markdown"
        export_archive_path = None
        latest_export_path = None
        if export_md.is_dir():
            try:
                finish_dt = datetime.fromisoformat(finished_at)
                finish_stamp = finish_dt.astimezone().strftime("%Y-%m-%d_%H-%M-%S")
            except Exception:
                finish_stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            safe_candidate = re.sub(r"[^A-Za-z0-9._-]+", "_", args.candidate).strip("_")
            archive_dir = export_md / "Jho Lite Runs"
            archive_dir.mkdir(parents=True, exist_ok=True)
            export_archive_path = archive_dir / (
                f"JHO_LITE_{finish_stamp}_{safe_candidate}_{trace_id[:8]}.md"
            )
            shutil.copy2(report_path, export_archive_path)
            latest_export_path = export_md / "JHO_LITE_LAST_RUN.md"
            shutil.copy2(report_path, latest_export_path)

        outputs = Path.home() / "wsl-files" / "Outputs" / "Agent Runs"
        if outputs.is_dir():
            dest = outputs / run_id
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(out, dest)

        print()
        print("=== JHO LITE RUN COMPLETE ===")
        print(f"status={status}")
        print(f"candidate={args.candidate}")
        print(f"harness=agy-headless")
        print(f"wire_model={agy_result.get('wire_model') or model}")
        print(f"applied_config={json.dumps(applied_config, ensure_ascii=False)}")
        print(f"duration_seconds={duration:.1f}")
        print(f"integrity={'PASS' if integrity_ok else 'FAIL'}")
        print(f"permission_blocked={agy_result.get('permission_blocked')}")
        print(f"outside_workspace_notice={agy_result.get('outside_workspace_notice')}")
        print(f"agy_init_cwd={(agy_result.get('init') or {}).get('cwd')}")
        print(f"notification_success={notification.get('success')}")
        if notification.get("reason"):
            print(f"notification_reason={notification.get('reason')}")
        print(f"report={report_path}")
        if export_archive_path is not None:
            print(f"export_archive={export_archive_path}")
        if latest_export_path is not None:
            print(f"export_latest={latest_export_path}")
        if error:
            print(f"error={error}")

        if not integrity_ok:
            return 7
        return 0 if status == "completed" else 6

    # Conservative policy: readonly both as primary policy and ceiling.
    spec = {
        "name": f"jho-{pool}-{model}-{effort}",
        "command": exe,
        "args": cmd_args,
        "workdir": str(workdir),
        "env": None,
        "env_remove": [],
        "permissions": "readonly",
        "allow_kinds": [],
        "deny_kinds": [],
        "permissions_ceiling": "readonly",
        "conversation_key": "",
        "timeout_s": timeout,
    }


    async def on_tool(event: dict) -> None:
        counters["tool_events"] += 1
        # Keep only low-risk metadata, not tool output bodies.
        summary = {}
        for key in ("type", "kind", "title", "status", "toolCallId", "tool_call_id"):
            val = event.get(key)
            if val is not None and isinstance(val, (str, int, float, bool)):
                summary[key] = str(val)[:300]
        if summary:
            tool_summaries.append(summary)
        progress.tool(counters["tool_events"], summary)

    async def on_thought(_delta: str) -> None:
        # Never persist or print chain-of-thought text.
        counters["thought_chunks"] += 1
        progress.activity("reasoning")

    async def on_text(_delta: str) -> None:
        counters["text_chunks"] += 1
        progress.activity("answer_stream")

    client = ConfiguredAcpClient(
        exe,
        cmd_args,
        cwd=str(workdir),
        name=spec["name"],
        permission=_make_permission(spec),
        session_id_path=None,
        record_runs=True,
        desired_model=wire_model,
        desired_effort=effort,
        status_hook=progress.phase,
    )

    tapped: TappedResult | None = None
    answer = ""
    error: str | None = None
    status = "failed"

    heartbeat_task = asyncio.create_task(
        progress.heartbeat(counters=counters, client_getter=lambda: client)
    )
    try:
        progress.phase("agent_start", "launching ACP agent")
        reply = await client.prompt(
            task,
            tool_callback=on_tool,
            thought_callback=on_thought,
            text_callback=on_text,
            timeout=timeout,
        )
        tapped = client.tapped_result(reply)
        answer = reply
        status = "completed"
        progress.phase("agent_finished", f"stop_reason={tapped.stop_reason}")
    except asyncio.CancelledError:
        progress.phase("cancelled", "killing ACP child process")
        client.kill_now()
        raise
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        progress.phase("error", error[:240])
    finally:
        progress.phase("teardown", "closing ACP session/process")
        try:
            await client.close()
        except Exception:
            pass
        progress.stop()
        try:
            await heartbeat_task
        except Exception:
            pass

    duration = time.monotonic() - started_mono
    progress.phase("integrity_check", "verifying HEAD and working-tree status")
    after_head = git_head(workdir)
    after_status = git_status(workdir)
    integrity_ok = (before_head == after_head) and (before_status == after_status)
    if not integrity_ok:
        status = "integrity_breach"

    finished_at = utc_now()

    notification = notify_windows_best_effort(
        status=status,
        candidate=args.candidate,
        duration_seconds=duration,
        integrity_ok=integrity_ok,
        enabled=not args.no_notify,
    )
    progress._print(
        "notification="
        + ("sent" if notification.get("success") else str(notification.get("reason") or "not-sent"))
    )

    result = {
        "run_id": run_id,
        "trace_id": trace_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": round(duration, 3),
        "candidate": args.candidate,
        "wire_model": wire_model,
        "applied_config": client.applied_config,
        "execution_class": args.execution_class,
        "task_class": args.task_class,
        "risk": args.risk,
        "role": args.role,
        "workdir": str(workdir),
        "permissions": "readonly",
        "status": status,
        "error": error,
        "integrity": {
            "ok": integrity_ok,
            "head_before": before_head,
            "head_after": after_head,
            "status_before": before_status,
            "status_after": after_status,
        },
        "signals": serialize_tapped(tapped) if tapped else None,
        "stream_counters": counters,
        "tool_summaries": tool_summaries[:200],
        "notification": notification,
        "answer": answer,
    }
    (out / "result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    (out / "answer.md").write_text(answer, encoding="utf-8")
    (out / "task.md").write_text(task, encoding="utf-8")

    report_answer = redact_text(answer)
    report = [
        "# Jho Lite Run Report",
        "",
        f"- Run: `{run_id}`",
        f"- Trace: `{trace_id}`",
        f"- Started: `{started_at}`",
        f"- Finished: `{finished_at}`",
        f"- Candidate: `{args.candidate}`",
        f"- Wire model: `{wire_model}`",
        f"- Applied config: `{json.dumps(client.applied_config, ensure_ascii=False)}`",
        f"- Role: `{args.role}`",
        f"- Task class: `{args.task_class}`",
        f"- Risk: `{args.risk}`",
        f"- Execution class: `{args.execution_class}`",
        f"- Duration: `{duration:.1f}s`",
        f"- Status: **{status}**",
        f"- Repository integrity: **{'PASS' if integrity_ok else 'FAIL'}**",
        f"- Stop reason: `{tapped.stop_reason if tapped else None}`",
        f"- Dead end: `{tapped.dead_end if tapped else None}`",
        f"- Tool events: `{counters['tool_events']}`",
        f"- Thought chunks observed (content not stored): `{counters['thought_chunks']}`",
        f"- Windows notification: `{'SENT' if notification.get('success') else 'NOT SENT'}`",
        "",
        "## Task",
        "",
        task,
        "",
        "## Agent answer",
        "",
        report_answer or "_No answer returned._",
    ]
    if error:
        report += ["", "## Error", "", f"`{redact_text(error)}`"]

    report_path = out / "RUN_REPORT.md"
    report_path.write_text("\n".join(report), encoding="utf-8")

    completed_event = {
        **base_event,
        "event_type": "run_finished",
        "timestamp": finished_at,
        "duration_seconds": round(duration, 3),
        "tests_passed": None,
        "validator_result": "repo_integrity_pass" if integrity_ok else "repo_integrity_fail",
        "review_result": None,
        "review_findings_count": None,
        "attempt": 1,
        "rework_count": 0,
        "first_pass_success": status == "completed" and integrity_ok,
        "final_success": status == "completed" and integrity_ok,
        "failure_category": None if status == "completed" else ("INTEGRITY" if not integrity_ok else "HARNESS_OR_AGENT"),
        "failure_attribution": None,
        "status": status,
        "notes": error,
    }
    append_jsonl(TELEMETRY_PATH, completed_event)

    latest = Path.home() / "agent-runs" / "JHO-LITE-RUN-LATEST"
    try:
        if latest.is_symlink() or latest.exists():
            latest.unlink()
        latest.symlink_to(out)
    except Exception:
        pass

    export_md = Path.home() / "wsl-files" / "Exports" / "Markdown"
    export_archive_path = None
    latest_export_path = None
    if export_md.is_dir():
        # Never overwrite historical exports. Include local finish time, candidate,
        # and trace suffix for readability + collision resistance.
        try:
            finish_dt = datetime.fromisoformat(finished_at)
            finish_stamp = finish_dt.astimezone().strftime("%Y-%m-%d_%H-%M-%S")
        except Exception:
            finish_stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

        safe_candidate = re.sub(r"[^A-Za-z0-9._-]+", "_", args.candidate).strip("_")
        archive_dir = export_md / "Jho Lite Runs"
        archive_dir.mkdir(parents=True, exist_ok=True)

        export_name = (
            f"JHO_LITE_{finish_stamp}_{safe_candidate}_{trace_id[:8]}.md"
        )
        export_archive_path = archive_dir / export_name
        shutil.copy2(report_path, export_archive_path)

        # Keep a convenience pointer/copy for quick access. This one intentionally
        # changes every run; the immutable archive above is the historical record.
        latest_export_path = export_md / "JHO_LITE_LAST_RUN.md"
        shutil.copy2(report_path, latest_export_path)

    outputs = Path.home() / "wsl-files" / "Outputs" / "Agent Runs"
    if outputs.is_dir():
        dest = outputs / run_id
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(out, dest)

    print()
    print("=== JHO LITE RUN COMPLETE ===")
    print(f"status={status}")
    print(f"candidate={args.candidate}")
    print(f"wire_model={wire_model}")
    print(f"applied_config={json.dumps(client.applied_config, ensure_ascii=False)}")
    print(f"duration_seconds={duration:.1f}")
    print(f"integrity={'PASS' if integrity_ok else 'FAIL'}")
    print(f"notification_success={notification.get('success')}")
    if notification.get("reason"):
        print(f"notification_reason={notification.get('reason')}")
    print(f"report={report_path}")
    if export_archive_path is not None:
        print(f"export_archive={export_archive_path}")
    if latest_export_path is not None:
        print(f"export_latest={latest_export_path}")
    if error:
        print(f"error={error}")

    if not integrity_ok:
        return 7
    return 0 if status == "completed" else 6


def list_candidates() -> int:
    reg = load_registry()
    mapping = [
        ("codex", "codex"),
        ("agy", "agy"),
        ("opencode-free", "opencode_free"),
        ("opencode-go", "opencode_go"),
        ("nvidia", "nvidia_background_long"),
    ]
    for display, key in mapping:
        print(f"[{display}]")
        for row in reg["pools"].get(key) or []:
            for effort in row.get("efforts") or []:
                suffix = ""
                if display == "agy":
                    suffix = ""
                if display == "nvidia":
                    suffix = "  # background_long only"
                print(f"  {display}:{row['model']}@{effort}{suffix}")
        print()
    return 0


def check() -> int:
    ensure_pinned_upstream()
    reg = load_registry()
    print("Jho Lite v0.2.5 preflight")
    print(f"protoAgent version: {proto_version()}")
    print(f"protoAgent HEAD: {git_head(PROTO_ROOT)}")
    print(f"registry: {REGISTRY_PATH}")
    print(f"routing: {ROUTING_PATH}")
    print(f"telemetry: {TELEMETRY_PATH}")
    print(f"codex-acp: {shutil.which('codex-acp') or 'MISSING'}")
    print(f"opencode: {shutil.which('opencode') or 'MISSING'}")
    print(f"agy: {shutil.which('agy') or 'MISSING'} (direct headless adapter enabled)")
    agy_cfg = agy_settings_summary()
    print(
        "agy policy: "
        f"toolPermission={agy_cfg.get('toolPermission')} "
        f"sandbox={agy_cfg.get('enableTerminalSandbox')} "
        f"ask_command_all={agy_cfg.get('ask_command_all', False)}"
    )
    product_read_rule = f"read_file({DEFAULT_PRODUCT.resolve()})"
    try:
        raw = json.loads(AGY_SETTINGS.read_text(encoding="utf-8")) if AGY_SETTINGS.exists() else {}
        agy_allow = ((raw.get("permissions") or {}).get("allow") or [])
        print(f"agy product read allow: {product_read_rule in agy_allow} ({product_read_rule})")
    except Exception:
        print("agy product read allow: UNKNOWN (settings parse failed)")
    notify_cap = windows_notification_capability()
    print(
        "windows notifications: "
        f"available={notify_cap.get('available')} "
        f"backend={notify_cap.get('backend')} "
        f"version={notify_cap.get('version', '-')}"
    )
    if not notify_cap.get("available"):
        print(f"windows notifications reason: {notify_cap.get('reason')}")
    print(f"active pools: {', '.join(reg.get('pools', {}).keys())}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="jho-lite", description="Jho Orchestrator Lite runner v0.2.5")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check", help="validate pinned runtime and local harnesses")
    sub.add_parser("candidates", help="list registry candidate configurations")
    sub.add_parser("notify-test", help="send a Windows toast through WSL interoperability")

    r = sub.add_parser("run", help="run one explicit read-only candidate")
    r.add_argument("--candidate", required=True, help="pool:model@effort")
    group = r.add_mutually_exclusive_group(required=True)
    group.add_argument("--task")
    group.add_argument("--task-file")
    r.add_argument("--task-id")
    r.add_argument("--task-class", default="repo_exploration")
    r.add_argument("--risk", default="low")
    r.add_argument("--complexity", default="low")
    r.add_argument("--role", default="worker")
    r.add_argument("--execution-class", choices=("interactive", "background_long"), default="interactive")
    r.add_argument("--workdir", default=str(DEFAULT_PRODUCT))
    r.add_argument("--timeout", default="900", help="seconds; NVIDIA defaults to 14400 when left at 900")
    r.add_argument(
        "--progress",
        choices=("quiet", "normal", "verbose"),
        default="normal",
        help="terminal progress verbosity; normal shows phases, tools and heartbeats",
    )
    r.add_argument(
        "--heartbeat",
        default="10",
        help="seconds between liveness heartbeats (default: 10)",
    )
    r.add_argument(
        "--stall-warn",
        default="45",
        help="warn when no ACP activity is seen for this many seconds (default: 45)",
    )
    r.add_argument(
        "--no-notify",
        action="store_true",
        help="disable the Windows completion notification for this run",
    )
    r.add_argument("--allow-dirty", action="store_true")

    return p


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.cmd == "check":
            return check()
        if args.cmd == "candidates":
            return list_candidates()
        if args.cmd == "notify-test":
            return notification_test()
        if args.cmd == "run":
            return asyncio.run(execute(args))
        return 2
    except (PolicyError, CapabilityError, AcpError) as exc:
        print(f"FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 4
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
