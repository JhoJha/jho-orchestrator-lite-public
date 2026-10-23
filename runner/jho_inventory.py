#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Installed beside jho_lite.py in protoAgent/local_jho/runner.
try:
    import jho_lite as jl
except Exception as exc:  # pragma: no cover - runtime guard
    print(f"FAIL: cannot import installed jho_lite runtime: {type(exc).__name__}: {exc}", file=sys.stderr)
    raise SystemExit(4)

PROTO_ROOT = jl.PROTO_ROOT
REGISTRY_PATH = jl.REGISTRY_PATH
DEFAULT_PRODUCT = jl.DEFAULT_PRODUCT
SNAPSHOT_DIR = PROTO_ROOT / "local_jho" / "telemetry" / "inventory_snapshots"
LATEST_SNAPSHOT = SNAPSHOT_DIR / "latest.json"
DEFAULT_EXPORT_DIR = Path.home() / "wsl-files" / "Exports" / "Markdown"

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
MODEL_LINE_RE = re.compile(r"^[A-Za-z0-9_.-]+/.+$")


@dataclass
class ProbeResult:
    provider: str
    status: str
    duration_seconds: float
    command: str | None
    version: str | None = None
    auth_summary: dict[str, Any] | None = None
    models: list[dict[str, Any]] | None = None
    error: str | None = None
    timed_out: bool = False


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stamp_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def run_cmd(args: list[str], *, cwd: Path | None = None, timeout: float = 20.0) -> tuple[int | None, str, str, bool, float]:
    started = time.monotonic()
    try:
        p = subprocess.run(args, cwd=cwd, text=True, capture_output=True, timeout=timeout)
        return p.returncode, p.stdout or "", p.stderr or "", False, time.monotonic() - started
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        err = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return None, out, err, True, time.monotonic() - started
    except Exception as exc:
        return None, "", f"{type(exc).__name__}: {exc}", False, time.monotonic() - started


def safe_text(text: str) -> str:
    return jl.redact_text(ANSI_RE.sub("", text or ""))


def load_registry() -> dict[str, Any]:
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))


def static_model_sets(reg: dict[str, Any]) -> dict[str, set[str]]:
    return {
        "codex": {str(x.get("model")) for x in reg.get("pools", {}).get("codex", [])},
        "agy": {str(x.get("model")) for x in reg.get("pools", {}).get("agy", [])},
        "opencode-free": {str(x.get("model")) for x in reg.get("pools", {}).get("opencode_free", [])},
        "opencode-go": {str(x.get("model")) for x in reg.get("pools", {}).get("opencode_go", [])},
        "nvidia": {str(x.get("model")) for x in reg.get("pools", {}).get("nvidia_background_long", [])},
    }


def static_efforts(reg: dict[str, Any], pool_key: str, model: str) -> list[str]:
    key_map = {
        "codex": "codex",
        "agy": "agy",
        "opencode-free": "opencode_free",
        "opencode-go": "opencode_go",
        "nvidia": "nvidia_background_long",
    }
    rows = reg.get("pools", {}).get(key_map[pool_key], [])
    for row in rows:
        if str(row.get("model")) == model:
            return [str(x) for x in (row.get("efforts") or [])]
    return []


def model_row(*, pool: str, model: str, efforts: list[str] | None, source: str, known: bool, eligible: bool = True, reason: str | None = None, raw_id: str | None = None) -> dict[str, Any]:
    return {
        "pool": pool,
        "model": model,
        "efforts": efforts or [],
        "source": source,
        "known_static": known,
        "state": "AVAILABLE" if known else "DISCOVERED_UNBENCHMARKED",
        "eligible": eligible,
        "eligibility_reason": reason,
        "raw_id": raw_id,
    }


def discover_opencode(reg: dict[str, Any], refresh: bool) -> ProbeResult:
    exe = shutil.which("opencode")
    if not exe:
        return ProbeResult("opencode", "UNAVAILABLE", 0.0, None, error="opencode binary not found")

    rc_v, out_v, err_v, to_v, dur_v = run_cmd([exe, "--version"], timeout=10)
    version = safe_text((out_v or err_v).strip()) or None

    rc_a, out_a, err_a, to_a, dur_a = run_cmd([exe, "auth", "list"], timeout=20)
    auth_raw = safe_text((out_a or "") + "\n" + (err_a or ""))
    auth_summary = {
        "opencode_go": "OpenCode Go" in auth_raw,
        "nvidia": bool(re.search(r"\bNvidia\b", auth_raw, re.I)),
    }

    cmd = [exe, "models"] + (["--refresh"] if refresh else [])
    rc, out, err, timed_out, dur = run_cmd(cmd, timeout=90)
    total_dur = dur_v + dur_a + dur
    if timed_out:
        return ProbeResult("opencode", "DEGRADED", total_dur, " ".join(cmd), version=version, auth_summary=auth_summary, error="model inventory timed out", timed_out=True)
    if rc != 0:
        return ProbeResult("opencode", "DEGRADED", total_dur, " ".join(cmd), version=version, auth_summary=auth_summary, error=safe_text((err or out)[-2000:]))

    static = static_model_sets(reg)
    rows: list[dict[str, Any]] = []
    for raw in safe_text(out).splitlines():
        raw = raw.strip()
        if not raw or raw.startswith("Models cache refreshed") or not MODEL_LINE_RE.match(raw):
            continue
        prefix, model = raw.split("/", 1)
        if prefix == "opencode":
            pool = "opencode-free"
        elif prefix == "opencode-go":
            pool = "opencode-go"
        elif prefix == "nvidia":
            pool = "nvidia"
        else:
            # Future configured OpenCode providers should still be preserved.
            pool = f"opencode-provider:{prefix}"

        known = model in static.get(pool, set())
        efforts = static_efforts(reg, pool, model) if pool in static else []
        eligible = True
        reason = None
        lower = model.lower()
        if pool.startswith("opencode") and (lower.startswith("gpt-") or "/gpt-" in lower):
            eligible = False
            reason = "RULE-002: GPT is not routed through OpenCode"
        elif pool == "nvidia" and not known:
            eligible = False
            reason = "new NVIDIA models are discovery-only until explicitly admitted; NVIDIA remains background_long"
        elif pool.startswith("opencode-provider:"):
            eligible = False
            reason = "new provider discovered; requires policy admission before routing"

        rows.append(model_row(pool=pool, model=model, efforts=efforts, source="opencode models", known=known, eligible=eligible, reason=reason, raw_id=raw))

    return ProbeResult("opencode", "AVAILABLE", total_dur, " ".join(cmd), version=version, auth_summary=auth_summary, models=rows)


def parse_agy_slug(slug: str) -> tuple[str, str]:
    for effort in ("high", "medium", "low"):
        suffix = f"-{effort}"
        if slug.endswith(suffix):
            return slug[: -len(suffix)], effort
    if slug == "claude-sonnet-4-6":
        return slug, "thinking"
    if slug == "claude-opus-4-6-thinking":
        return slug, "thinking"
    return slug, "default"


def discover_agy(reg: dict[str, Any]) -> ProbeResult:
    exe = shutil.which("agy")
    if not exe:
        return ProbeResult("agy", "UNAVAILABLE", 0.0, None, error="agy binary not found")
    rc_v, out_v, err_v, to_v, dur_v = run_cmd([exe, "--version"], timeout=10)
    version = safe_text((out_v or err_v).strip()) or None
    rc, out, err, timed_out, dur = run_cmd([exe, "models"], timeout=45)
    total = dur_v + dur
    if timed_out:
        return ProbeResult("agy", "DEGRADED", total, f"{exe} models", version=version, error="agy models timed out", timed_out=True)
    if rc != 0:
        return ProbeResult("agy", "DEGRADED", total, f"{exe} models", version=version, error=safe_text((err or out)[-2000:]))

    static = static_model_sets(reg)["agy"]
    grouped: dict[str, dict[str, Any]] = {}
    for line in safe_text(out).splitlines():
        line = line.strip()
        if not line:
            continue
        slug = line.split()[0]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", slug):
            continue
        model, effort = parse_agy_slug(slug)
        key = model
        row = grouped.setdefault(key, model_row(pool="agy", model=model, efforts=[], source="agy models", known=model in static, raw_id=slug))
        if effort not in row["efforts"]:
            row["efforts"].append(effort)
    rows = sorted(grouped.values(), key=lambda x: x["model"])
    return ProbeResult("agy", "AVAILABLE", total, f"{exe} models", version=version, models=rows)


class DiscoveryAcpClient(jl.AcpClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.jho_config_options: list[dict[str, Any]] = []

    async def _request(self, method: str, params: dict, *, timeout: float = 120.0):
        result = await super()._request(method, params, timeout=timeout)
        if method == "session/new":
            self.jho_config_options = jl.config_options(result)
        return result


async def _discover_codex_async(reg: dict[str, Any], workdir: Path) -> ProbeResult:
    exe = shutil.which("codex-acp")
    codex = shutil.which("codex")
    if not exe:
        return ProbeResult("codex", "UNAVAILABLE", 0.0, None, error="codex-acp binary not found")

    version = None
    dur_version = 0.0
    if codex:
        rc_v, out_v, err_v, to_v, dur_version = run_cmd([codex, "--version"], timeout=10)
        version = safe_text((out_v or err_v).strip()) or None

    started = time.monotonic()
    spec = {
        "permissions": "readonly",
        "allow_kinds": [],
        "deny_kinds": [],
        "permissions_ceiling": "readonly",
    }
    client = DiscoveryAcpClient(
        exe,
        [],
        cwd=str(workdir),
        name="jho-codex-discovery",
        permission=jl._make_permission(spec),
        session_id_path=None,
        record_runs=False,
    )
    try:
        # _start() performs initialize + session/new but sends no model prompt/inference.
        await asyncio.wait_for(client._start(), timeout=45.0)
        opts = list(client.jho_config_options)
        model_opt = jl.find_option(opts, category="model", ids=("model",))
        if not model_opt:
            raise jl.CapabilityError("Codex ACP session/new exposed no model config option")
        models = jl.option_values(model_opt)
        static = static_model_sets(reg)["codex"]
        rows: list[dict[str, Any]] = []
        for model in models:
            eligible = model != "gpt-6-astra"
            reason = None if eligible else "RULE-001: Astra is denied by policy"
            efforts: list[str] = []
            try:
                response = await asyncio.wait_for(
                    client._request(
                        "session/set_config_option",
                        {"sessionId": client._session_id, "configId": model_opt["id"], "value": model},
                        timeout=15.0,
                    ),
                    timeout=20.0,
                )
                updated = jl.config_options(response)
                effort_opt = jl.find_option(updated or opts, category="thought_level", ids=("reasoning_effort", "effort"))
                efforts = jl.option_values(effort_opt)
            except Exception:
                efforts = static_efforts(reg, "codex", model)
            rows.append(model_row(pool="codex", model=model, efforts=efforts, source="codex-acp session config", known=model in static, eligible=eligible, reason=reason, raw_id=model))
        return ProbeResult("codex", "AVAILABLE", dur_version + (time.monotonic() - started), exe, version=version, models=rows)
    except asyncio.TimeoutError:
        return ProbeResult("codex", "DEGRADED", dur_version + (time.monotonic() - started), exe, version=version, error="Codex ACP discovery timed out", timed_out=True)
    except Exception as exc:
        return ProbeResult("codex", "DEGRADED", dur_version + (time.monotonic() - started), exe, version=version, error=f"{type(exc).__name__}: {safe_text(str(exc))}")
    finally:
        try:
            await asyncio.wait_for(client.close(), timeout=8.0)
        except Exception:
            client.kill_now()


def discover_codex(reg: dict[str, Any], workdir: Path) -> ProbeResult:
    return asyncio.run(_discover_codex_async(reg, workdir))


def flatten_models(probes: list[ProbeResult]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for p in probes:
        rows.extend(p.models or [])
    return rows


def inventory_key(row: dict[str, Any]) -> str:
    return f"{row.get('pool')}:{row.get('model')}"


def load_previous_snapshot() -> dict[str, Any] | None:
    try:
        return json.loads(LATEST_SNAPSHOT.read_text(encoding="utf-8")) if LATEST_SNAPSHOT.exists() else None
    except Exception:
        return None


def compute_diff(previous: dict[str, Any] | None, current_models: list[dict[str, Any]]) -> dict[str, list[str]]:
    cur = {inventory_key(x) for x in current_models}
    if not previous:
        return {"added": sorted(cur), "removed": [], "unchanged": []}
    prev = {inventory_key(x) for x in previous.get("models", [])}
    return {
        "added": sorted(cur - prev),
        "removed": sorted(prev - cur),
        "unchanged": sorted(cur & prev),
    }


def snapshot_id(payload_without_id: dict[str, Any]) -> str:
    blob = json.dumps(payload_without_id, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def markdown_report(snapshot: dict[str, Any]) -> str:
    lines = [
        "# Jho v0.3 — Capability Inventory Report",
        "",
        f"- Snapshot: `{snapshot['snapshot_id']}`",
        f"- Captured at: `{snapshot['captured_at']}`",
        f"- Product workdir: `{snapshot['workdir']}`",
        f"- Static registry: `{snapshot['registry_path']}`",
        "",
        "## Provider health",
        "",
        "| Provider | Status | Version | Models discovered | Duration |",
        "|---|---|---|---:|---:|",
    ]
    for p in snapshot["providers"]:
        lines.append(f"| {p['provider']} | **{p['status']}** | `{p.get('version') or '-'}` | {len(p.get('models') or [])} | {p['duration_seconds']:.2f}s |")
    lines += ["", "## Inventory changes", ""]
    diff = snapshot["diff"]
    if not diff["added"] and not diff["removed"]:
        lines.append("No model inventory changes from the previous snapshot.")
    else:
        if diff["added"]:
            lines.append("### Added")
            lines.extend(f"- `+ {x}`" for x in diff["added"])
            lines.append("")
        if diff["removed"]:
            lines.append("### Removed")
            lines.extend(f"- `- {x}`" for x in diff["removed"])
            lines.append("")

    lines += ["## Models by pool", ""]
    pools: dict[str, list[dict[str, Any]]] = {}
    for row in snapshot["models"]:
        pools.setdefault(row["pool"], []).append(row)
    for pool in sorted(pools):
        rows = sorted(pools[pool], key=lambda x: x["model"])
        lines.append(f"### {pool}")
        lines.append("")
        lines.append("| Model | State | Eligible | Efforts | Source |")
        lines.append("|---|---|---|---|---|")
        for row in rows:
            eligible = "yes" if row.get("eligible") else "no"
            efforts = ", ".join(row.get("efforts") or []) or "unknown"
            model = str(row["model"]).replace("|", "\\|")
            source = str(row.get("source") or "-").replace("|", "\\|")
            lines.append(f"| `{model}` | {row['state']} | {eligible} | `{efforts}` | {source} |")
            if row.get("eligibility_reason"):
                lines.append(f"| ↳ policy |  |  |  | {row['eligibility_reason']} |")
        lines.append("")

    errors = [p for p in snapshot["providers"] if p.get("error")]
    if errors:
        lines += ["## Probe errors / degradation", ""]
        for p in errors:
            lines.append(f"- **{p['provider']}**: `{p['error']}`")
        lines.append("")

    lines += [
        "## Interpretation rules",
        "",
        "- `AVAILABLE`: live discovery succeeded.",
        "- `DISCOVERED_UNBENCHMARKED`: model exists live but was not in the static admitted registry.",
        "- New providers/models are not automatically trusted for high-risk routing.",
        "- GPT discovered through OpenCode remains in telemetry but is ineligible by RULE-002.",
        "- Newly discovered NVIDIA models remain discovery-only until explicitly admitted; NVIDIA stays background-long.",
        "- Historical model identities are preserved even when a later snapshot removes them.",
        "",
        "## Next action",
        "",
        "Upload this Markdown report back to ChatGPT. Do not manually edit the report; it is the evidence artifact for the next implementation slice.",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(prog="jho-lite inventory", description="Jho v0.3 capability discovery slice")
    ap.add_argument("--workdir", default=str(DEFAULT_PRODUCT))
    ap.add_argument("--no-refresh", action="store_true", help="do not force OpenCode model cache refresh")
    ap.add_argument("--report-out", help="optional explicit Markdown output path")
    args = ap.parse_args()

    jl.ensure_pinned_upstream()
    reg = load_registry()
    workdir = Path(args.workdir).expanduser().resolve()
    if not workdir.is_dir():
        print(f"FAIL: workdir not found: {workdir}", file=sys.stderr)
        return 4

    previous = load_previous_snapshot()

    print("============================================================")
    print(" JHO v0.3 — CAPABILITY INVENTORY")
    print("============================================================")

    probes: list[ProbeResult] = []
    for name, fn in [
        ("codex", lambda: discover_codex(reg, workdir)),
        ("opencode", lambda: discover_opencode(reg, not args.no_refresh)),
        ("agy", lambda: discover_agy(reg)),
    ]:
        print(f"[probe] {name} ...", flush=True)
        try:
            result = fn()
        except Exception as exc:
            result = ProbeResult(name, "DEGRADED", 0.0, None, error=f"{type(exc).__name__}: {safe_text(str(exc))}")
        probes.append(result)
        print(f"[probe] {name}: {result.status} models={len(result.models or [])} duration={result.duration_seconds:.2f}s", flush=True)
        if result.error:
            print(f"        reason={result.error}", flush=True)

    models = flatten_models(probes)
    diff = compute_diff(previous, models)
    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "captured_at": utc_now(),
        "workdir": str(workdir),
        "registry_path": str(REGISTRY_PATH),
        "protoagent_version": jl.proto_version(),
        "protoagent_head": jl.git_head(PROTO_ROOT),
        "providers": [asdict(x) for x in probes],
        "models": models,
        "diff": diff,
    }
    payload["snapshot_id"] = snapshot_id(payload)

    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    snap_path = SNAPSHOT_DIR / f"inventory_{stamp_utc()}_{payload['snapshot_id']}.json"
    snap_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    LATEST_SNAPSHOT.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    report = markdown_report(payload)
    if args.report_out:
        report_path = Path(args.report_out).expanduser().resolve()
    else:
        out_dir = DEFAULT_EXPORT_DIR if DEFAULT_EXPORT_DIR.is_dir() else (Path.home() / "agent-runs")
        out_dir.mkdir(parents=True, exist_ok=True)
        report_path = out_dir / f"JHO_V0_3_CAPABILITY_INVENTORY_{stamp_utc()}_{payload['snapshot_id']}.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8")

    latest_md = report_path.parent / "JHO_V0_3_CAPABILITY_INVENTORY_LAST.md"
    latest_md.write_text(report, encoding="utf-8")

    print()
    print("=== INVENTORY COMPLETE ===")
    print(f"snapshot_id={payload['snapshot_id']}")
    print(f"models_total={len(models)}")
    print(f"added={len(diff['added'])}")
    print(f"removed={len(diff['removed'])}")
    print(f"snapshot_json={snap_path}")
    print(f"report_md={report_path}")
    print(f"report_latest={latest_md}")
    print("Upload report_md (or report_latest) back to ChatGPT.")

    # Inventory can still be useful if one provider degrades. Only fail if ALL probes fail.
    if all(p.status != "AVAILABLE" for p in probes):
        return 6
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
