"""Pilot hardening utilities (Build 8): readiness, diagnostics, gated live
probes, retention cleanup, and the redacted pilot manifest.

Everything here is operational tooling - no business logic from Builds 1-7
changes. Live probes are DISABLED by default and doubly gated: the
environment flag must be true AND the caller must confirm explicitly; they
are never triggered by rendering a page or running tests. All output is
redacted: no passwords, tokens, Authorization headers, or raw
credential-bearing requests/responses ever appear in reports, logs,
manifests, or the CLI.

CLI:
    python -m apps.web.transfer.pilot doctor [--deep]
    python -m apps.web.transfer.pilot auth-check --yes
    python -m apps.web.transfer.pilot product-check --yes \
        --org <ORGANIZATION-ID> --plu <ID>
        [--show-values]
    python -m apps.web.transfer.pilot cleanup [--dry-run | --execute]
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from apps.web.job_manager import JobError, utc_now
from apps.web.transfer import jobs
from apps.web.transfer.models import (
    JOB_EXTRACTING,
    JOB_PACKING_PREPARATION_IN_PROGRESS,
    JOB_PRODUCT_LOOKUP_IN_PROGRESS,
    JOB_WORKBOOK_GENERATION_IN_PROGRESS,
)

MANIFEST_SCHEMA_VERSION = 1
MANIFEST_DIR = "pilot"
MANIFEST_NAME = "result.json"

IN_PROGRESS_STATUSES = (JOB_EXTRACTING, JOB_PRODUCT_LOOKUP_IN_PROGRESS,
                        JOB_PACKING_PREPARATION_IN_PROGRESS,
                        JOB_WORKBOOK_GENERATION_IN_PROGRESS)

EXIT_READY = 0
EXIT_WARNINGS = 1
EXIT_BLOCKED = 2

_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


# --- pilot configuration ----------------------------------------------------------

@dataclass(frozen=True)
class PilotConfig:
    enable_live_auth: bool = False
    enable_live_product_lookup: bool = False


def pilot_config_problems() -> list[str]:
    problems = []
    for name in ("PILOT_ENABLE_LIVE_AUTH",
                 "PILOT_ENABLE_LIVE_PRODUCT_LOOKUP"):
        raw = _env(name)
        if raw and raw.lower() not in _TRUE + _FALSE:
            problems.append(f"{name} must be a boolean.")
    return problems


def load_pilot_config() -> PilotConfig:
    problems = pilot_config_problems()
    if problems:
        raise JobError("Pilot configuration invalid: " + " ".join(problems))
    return PilotConfig(
        enable_live_auth=_env("PILOT_ENABLE_LIVE_AUTH").lower() in _TRUE,
        enable_live_product_lookup=_env(
            "PILOT_ENABLE_LIVE_PRODUCT_LOOKUP").lower() in _TRUE)


# --- OCR readiness ----------------------------------------------------------------

def ocr_ready(*, deep: bool = False) -> dict:
    """Import-level OCR readiness; `deep=True` additionally runs a tiny
    synthetic image through the real engine (seconds of model init)."""
    try:
        import rapidocr_onnxruntime  # noqa: F401
        import onnxruntime           # noqa: F401
        import cv2                   # noqa: F401
    except ImportError as exc:
        return {"status": "missing",
                "detail": f"dependency not installed ({exc.name})"}
    if not deep:
        return {"status": "installed", "detail": "import check only"}
    try:
        import numpy as np
        from apps.web.transfer.ocr import RapidOcrAdapter
        adapter = RapidOcrAdapter()
        import cv2
        img = np.full((60, 200, 3), 255, dtype=np.uint8)
        cv2.putText(img, "PILOT OCR 123", (5, 40),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 2)
        ok, png = cv2.imencode(".png", img)
        tokens = adapter.recognize_page(png.tobytes())
        text = " ".join(t.text for t in tokens).upper()
        if "123" in text or "OCR" in text or "PILOT" in text:
            return {"status": "ready", "detail": "synthetic smoke passed"}
        return {"status": "degraded",
                "detail": "engine ran but did not read the smoke image"}
    except Exception as exc:
        return {"status": "failed", "detail": type(exc).__name__}


# --- doctor -----------------------------------------------------------------------

def _writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".probe-{os.getpid()}"
        probe.write_text("ok")
        probe.unlink()
        return True
    except OSError:
        return False


def _commit_hash() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True,
            text=True, timeout=10,
            cwd=Path(__file__).resolve().parents[3]).stdout.strip() or "?"
    except Exception:
        return "?"


def _job_state_counts() -> dict:
    counts: dict[str, int] = {}
    root = jobs.transfer_jobs_root()
    if not root.is_dir():
        return counts
    for entry in root.iterdir():
        if not jobs.TJOB_ID_RE.match(entry.name) or entry.is_symlink():
            continue
        job = jobs.load_transfer_job(entry.name)
        if job is not None:
            counts[job.status] = counts.get(job.status, 0) + 1
    return counts


def doctor(*, deep_ocr: bool = False) -> tuple[dict, int]:
    """Redacted operational diagnostic. Exit code: 0 ready, 1 warnings,
    2 blocking failure. Never performs a network call."""
    from apps.web.transfer import product_lookup as pl
    from apps.web.transfer import workbook as wbmod
    from apps.web.transfer.gateway_auth import config_problems as auth_problems
    from apps.web.transfer.jobs import workflow_enabled

    warnings: list[str] = []
    blockers: list[str] = []

    ocr = ocr_ready(deep=deep_ocr)
    if ocr["status"] in ("missing", "failed"):
        warnings.append("OCR is unavailable; scanned PDFs cannot be "
                        "processed (install requirements-ocr.txt).")

    jobs_root_ok = _writable(jobs.transfer_jobs_root())
    if not jobs_root_ok:
        blockers.append("Transfer job root is not writable.")

    api_problems = auth_problems() + pl.product_config_problems()
    api_status = ("configured" if not api_problems else
                  "not_configured" if all("is not set" in p
                                          for p in api_problems)
                  else "configuration_error")
    if api_status == "configuration_error":
        blockers.append("API configuration is invalid (see problems).")
    elif api_status == "not_configured":
        warnings.append("API Gateway credentials are not configured; live "
                        "product lookup is unavailable.")

    workbook_problems = wbmod.workbook_config_problems()
    if workbook_problems:
        blockers.append("Workbook configuration is invalid.")
    pilot_problems = pilot_config_problems()
    if pilot_problems:
        blockers.append("Pilot configuration is invalid.")

    state_counts = _job_state_counts()
    stuck = sum(state_counts.get(s, 0) for s in IN_PROGRESS_STATUSES)
    if stuck:
        warnings.append(f"{stuck} job(s) appear stranded in an in-progress "
                        "state; open them in the UI to retry safely.")

    try:
        usage = shutil.disk_usage(jobs.transfer_jobs_root())
        free_gb = usage.free / 1e9
        if free_gb < 1.0:
            warnings.append(f"Low disk space: {free_gb:.1f} GB free.")
    except OSError:
        free_gb = None

    try:
        cleanup_preview = cleanup(dry_run=True)
    except JobError:
        cleanup_preview = {"files": 0, "bytes": 0}

    config = load_pilot_config() if not pilot_problems else PilotConfig()
    report = {
        "generated_at": utc_now(),
        "python": sys.version.split()[0],
        "app_commit": _commit_hash(),
        "transfer_workflow_enabled": workflow_enabled(),
        "in_container": Path("/.dockerenv").exists(),
        "ocr": ocr,
        "job_root_writable": jobs_root_ok,
        "api_status": api_status,
        "api_problems": api_problems,        # variable NAMES only
        "workbook_config_problems": workbook_problems,
        "live_auth_enabled": config.enable_live_auth,
        "live_product_lookup_enabled": config.enable_live_product_lookup,
        "job_state_counts": state_counts,
        "stranded_jobs": stuck,
        "free_disk_gb": round(free_gb, 1) if free_gb is not None else None,
        "cleanup_candidates": {
            "files": cleanup_preview["files"],
            "bytes": cleanup_preview["bytes"],
        },
        "warnings": warnings,
        "blockers": blockers,
    }
    code = (EXIT_BLOCKED if blockers
            else EXIT_WARNINGS if warnings else EXIT_READY)
    return report, code


# --- retention cleanup ------------------------------------------------------------

def cleanup(*, dry_run: bool = True) -> dict:
    """Transfer-job cleanup across the transfer root ONLY: expired output
    workbooks/ZIPs, stray *.tmp-* files, and stale archived *-stale-*.json
    metadata beyond retention. Never touches invoice jobs (different root),
    in-progress jobs, symlinks, or anything outside the job root. Reports
    counts and bytes only."""
    from apps.web.transfer import workbook as wbmod
    config = wbmod.load_workbook_config()
    root = jobs.transfer_jobs_root()
    result = {"dry_run": dry_run, "files": 0, "bytes": 0, "jobs_scanned": 0,
              "jobs_skipped_active": 0, "errors": 0}
    if not root.is_dir():
        return result
    cutoff = time.time() - config.retention_hours * 3600
    for entry in sorted(root.iterdir()):
        if not jobs.TJOB_ID_RE.match(entry.name) or entry.is_symlink() \
                or not entry.is_dir():
            continue
        result["jobs_scanned"] += 1
        job = jobs.load_transfer_job(entry.name)
        if job is not None and job.status in IN_PROGRESS_STATUSES:
            result["jobs_skipped_active"] += 1
            continue
        candidates: list[Path] = []
        for pattern in ("**/*.tmp-*",):
            candidates.extend(entry.glob(pattern))
        output = entry / "output"
        if output.is_dir():
            for path in output.iterdir():
                if path.suffix.lower() in (".xlsx", ".zip") \
                        and not path.is_symlink():
                    try:
                        if path.stat().st_mtime < cutoff:
                            candidates.append(path)
                    except OSError:
                        result["errors"] += 1
        for sub in ("review", "product_lookup", "packing", "output"):
            folder = entry / sub
            if folder.is_dir():
                for path in folder.glob("*-stale-*.json"):
                    try:
                        if path.stat().st_mtime < cutoff \
                                and not path.is_symlink():
                            candidates.append(path)
                    except OSError:
                        result["errors"] += 1
        for path in candidates:
            try:
                resolved = path.resolve()
                if not str(resolved).startswith(str(root.resolve())):
                    result["errors"] += 1        # containment violation
                    continue
                if path.is_symlink():
                    continue
                size = path.stat().st_size
                if not dry_run:
                    path.unlink()
                result["files"] += 1
                result["bytes"] += size
            except OSError:
                result["errors"] += 1
    return result


# --- gated live probes ------------------------------------------------------------

def auth_probe(*, confirm: bool = False) -> dict:
    """ONE live login through the Build 4 client. Requires
    PILOT_ENABLE_LIVE_AUTH=true AND confirm=True. Reports redacted
    metadata only; in-memory tokens are cleared afterwards and nothing is
    persisted."""
    config = load_pilot_config()
    if not config.enable_live_auth:
        raise JobError("Live auth probe is disabled. Set "
                       "PILOT_ENABLE_LIVE_AUTH=true to allow it.")
    if not confirm:
        raise JobError("Live auth probe requires explicit confirmation "
                       "(--yes).")
    from apps.web.transfer.gateway_auth import AuthError, build_client
    client = build_client()
    started = time.monotonic()
    outcome = {"probe": "auth", "at": utc_now(), "success": False,
               "duration_seconds": None, "gateway_code": None,
               "http_status": None, "access_token_present": False,
               "refresh_token_present": False, "expires_in_seconds": None,
               "error_code": None}
    try:
        tokens = client.login()
        outcome.update(
            success=True,
            access_token_present=bool(tokens.access_token),
            refresh_token_present=bool(tokens.refresh_token),
            expires_in_seconds=(round(tokens.expires_at - tokens.obtained_at)
                                if tokens.expires_at else None))
    except AuthError as exc:
        outcome.update(error_code=exc.code, gateway_code=exc.gateway_code,
                       http_status=exc.http_status)
    except Exception as exc:            # transport-level failure, redacted
        outcome.update(error_code=type(exc).__name__)
    finally:
        outcome["duration_seconds"] = round(time.monotonic() - started, 2)
        client.clear_tokens()                    # never keep probe tokens
    return outcome


def observe_record_schema(records: list[dict]) -> dict:
    """Safe schema observation of product records: field names, JSON value
    types, and presence counts - NO values."""
    fields: dict[str, dict] = {}
    for record in records:
        for key, value in record.items():
            info = fields.setdefault(str(key), {"types": set(), "count": 0,
                                                "null_count": 0,
                                                "empty_count": 0})
            info["count"] += 1
            if value is None:
                info["null_count"] += 1
                info["types"].add("null")
            else:
                info["types"].add(type(value).__name__)
                if isinstance(value, str) and not value.strip():
                    info["empty_count"] += 1
    return {name: {"types": sorted(info["types"]), "count": info["count"],
                   "null_count": info["null_count"],
                   "empty_count": info["empty_count"]}
            for name, info in sorted(fields.items())}


def product_probe(*, org_id: str, plus: list[str],
                  confirm: bool = False,
                  show_values: bool = False, transport=None,
                  auth=None) -> dict:
    """ONE controlled itemMaster-get batch (max 3 identifiers) through the
    sanctioned Transfer client/endpoint configuration, using the given
    Organization ID (never derived from the login account). Requires
    PILOT_ENABLE_LIVE_PRODUCT_LOOKUP=true AND confirm=True. Captures a
    schema observation; business VALUES (never tokens) are included only
    with show_values=True for explicit business confirmation."""
    config = load_pilot_config()
    if not config.enable_live_product_lookup:
        raise JobError("Live product probe is disabled. Set "
                       "PILOT_ENABLE_LIVE_PRODUCT_LOOKUP=true to allow it.")
    if not confirm:
        raise JobError("Live product probe requires explicit confirmation "
                       "(--yes).")
    if not plus or len(plus) > 3:
        raise JobError("The product probe accepts 1-3 identifiers only.")
    from apps.web.transfer import organizations as orgs
    from apps.web.transfer import product_lookup as pl
    from apps.web.transfer.gateway_auth import build_client
    if not orgs.is_valid_org_id(org_id):
        raise JobError("Unknown Organization ID; use one from the "
                       "approved organization catalog.")
    org_id = str(org_id).strip()
    auth = auth or build_client()
    product_config = pl.load_product_config()
    client = pl.ProductGatewayClient(auth, product_config,
                                     transport=transport)
    keys = [pl.ProductLookupKey(org_id=org_id, plu=str(p).strip(),
                                identifier_type="PROBE") for p in plus]
    started = time.monotonic()
    observation = {"probe": "product", "at": utc_now(),
                   "request_count": len(keys),
                   "organization_id": org_id, "success": False,
                   "duration_seconds": None, "http_status": None,
                   "gateway_code": None, "records_returned": 0,
                   "correlated": 0, "omitted_identifiers": [],
                   "record_schema": {}, "top_level_fields": [],
                   "error_code": None, "values": None}
    try:
        outcome = client.lookup_batch(keys, 1)      # one batch, one attempt
        observation.update(
            success=True, http_status=outcome.http_status,
            gateway_code=outcome.gateway_code)
        records = outcome.records
        observation["records_returned"] = len(records)
        observation["record_schema"] = observe_record_schema(records)
        requested = {k.plu for k in keys}
        echoed = set()
        for record in records:
            plu = str(record.get("plu") or record.get("PLU") or "")
            ean = str(record.get("ean") or record.get("EAN") or "")
            if plu in requested or ean in requested:
                echoed.add(plu if plu in requested else ean)
        observation["correlated"] = len(echoed)
        observation["omitted_identifiers"] = sorted(requested - echoed)
        if show_values:
            observation["values"] = [
                {k: v for k, v in record.items()
                 if not pl_is_sensitive_key(k)} for record in records]
    except Exception as exc:
        observation["error_code"] = getattr(exc, "code",
                                            type(exc).__name__)
        # Diagnostic capture (Build 12): the typed ProductError already
        # carries the HTTP status, gateway business code, and a
        # redaction-safe message (never tokens, credentials, headers, or
        # raw bodies) - preserve them so a live rejection is diagnosable.
        observation["http_status"] = getattr(exc, "http_status", None)
        observation["gateway_code"] = getattr(exc, "gateway_code", None)
        observation["error_message"] = (str(exc)
                                        if hasattr(exc, "code") else None)
    finally:
        observation["duration_seconds"] = round(
            time.monotonic() - started, 2)
        try:
            auth.clear_tokens()
        except Exception:
            pass
    return observation


def pl_is_sensitive_key(key: str) -> bool:
    from apps.web.transfer.gateway_auth import _is_sensitive_key
    return _is_sensitive_key(str(key))


# --- pilot manifest ---------------------------------------------------------------

def manifest_path(job_id: str) -> Path:
    return jobs.transfer_job_dir_for(job_id) / MANIFEST_DIR / MANIFEST_NAME


def write_pilot_manifest(job_id: str) -> dict:
    """Redacted end-to-end pilot record built ONLY from existing artifacts:
    counts, hashes, statuses, safe issue codes. Never source PDFs, raw
    responses, tokens, or credentials."""
    from apps.web.transfer import packing as pk
    from apps.web.transfer import product_lookup as pl
    from apps.web.transfer import workbook as wbmod

    job = jobs.load_transfer_job(job_id)
    if job is None:
        raise JobError("Unknown transfer job id.")
    job_dir = jobs.transfer_job_dir_for(job_id)

    def sha(path: Path):
        try:
            return hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return None

    sources = [{"sequence": f.sequence, "sha256": f.sha256,
                "page_count": f.page_count, "size_bytes": f.size_bytes}
               for f in job.files]
    prepared = pk.load_preparation(job_id) or {}
    enrichment = pl.load_enrichment(job_id) or {}
    output = wbmod.load_output(job_id) or {}
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "job_id": job_id,
        "created_at": utc_now(),
        "app_commit": _commit_hash(),
        "job_status": job.status,
        "source_files": sources,
        "extraction": {
            "documents": len(sources),
        },
        "enrichment_summary": enrichment.get("summary") or {},
        "packing_summary": (prepared.get("summary") or {}),
        "destinations": [g.get("destination_code")
                         for g in prepared.get("destinations", [])],
        "delivery_invoice_numbers": [
            g.get("delivery_invoice_number")
            for g in prepared.get("destinations", [])],
        "workbooks": [{
            "destination_code": w.get("destination_code"),
            "filename": w.get("filename"),
            "sha256": w.get("sha256"),
            "byte_size": w.get("byte_size"),
            "validation_status": w.get("validation_status"),
        } for w in output.get("destination_workbooks", [])],
        "zip": ({"filename": output["zip"].get("filename"),
                 "sha256": output["zip"].get("sha256"),
                 "byte_size": output["zip"].get("byte_size")}
                if output.get("zip") else None),
        "issue_codes": sorted({i.get("code")
                               for src in (prepared, enrichment, output)
                               for i in src.get("issues", [])
                               if i.get("code")}),
        "configuration_fields": {
            "customer_style_field":
                wbmod.load_workbook_config().customer_style_field or None,
            "customer_color_code_field":
                wbmod.load_workbook_config().customer_color_code_field
                or None,
            "customer_color_desc_field":
                wbmod.load_workbook_config().customer_color_desc_field
                or None,
        },
    }
    path = manifest_path(job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{MANIFEST_NAME}.tmp-{os.getpid()}")
    tmp.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return manifest


# --- CLI --------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="pilot",
                                     description="Transfer pilot tooling")
    sub = parser.add_subparsers(dest="command", required=True)
    p_doctor = sub.add_parser("doctor")
    p_doctor.add_argument("--deep", action="store_true",
                          help="run the synthetic OCR smoke (slow)")
    p_auth = sub.add_parser("auth-check")
    p_auth.add_argument("--yes", action="store_true")
    p_prod = sub.add_parser("product-check")
    p_prod.add_argument("--yes", action="store_true")
    p_prod.add_argument("--org", required=True,
                        help="Organization ID from the approved catalog")
    p_prod.add_argument("--plu", action="append", required=True,
                        help="repeatable, max 3")
    p_prod.add_argument("--show-values", action="store_true")
    p_clean = sub.add_parser("cleanup")
    group = p_clean.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", default=True)
    group.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "doctor":
        report, code = doctor(deep_ocr=args.deep)
        print(json.dumps(report, indent=2))
        return code
    if args.command == "auth-check":
        print(json.dumps(auth_probe(confirm=args.yes), indent=2))
        return 0
    if args.command == "product-check":
        print(json.dumps(product_probe(
            org_id=args.org, plus=args.plu, confirm=args.yes,
            show_values=args.show_values), indent=2))
        return 0
    if args.command == "cleanup":
        result = cleanup(dry_run=not args.execute)
        print(json.dumps(result, indent=2))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
