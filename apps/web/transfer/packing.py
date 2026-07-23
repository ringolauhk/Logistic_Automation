"""Packing preparation (Build 6): destination grouping, carton
resequencing, and same-carton consolidation over the Build 5 enrichment.

Inputs are strictly the CURRENT approved review plus the CURRENT product
enrichment (checksum-guarded); raw extraction values are never used when a
correction exists, and none of the upstream artifacts are ever modified.
Output is one destination package per future workbook, persisted atomically
as packing/result.json. NO Excel, ZIP, printing, customer attribute
mapping, or API call exists here.

Key rules implemented:
  * grouping by effective To Loc. (never filenames); destination order =
    first appearance in upload -> page -> line order;
  * carton identity = the Build 3 carton entity (unique per document), so
    original carton "001" in two files stays distinct; a carton spanning
    pages remains one carton ordered by its first page;
  * generated numbers restart at 001 per destination (pad width 3, growing
    naturally past 999 - never wrapping); originals kept alongside;
  * identical API item+color+size(+EAN/PLU) lines combine ONLY within one
    generated carton - never across cartons or destinations, never by
    description alone; contributing line IDs stay traceable;
  * one deterministic delivery invoice number per destination
    (PL-<DEST>-<YYYYMMDD>-<seq>), carried forward verbatim when inputs are
    unchanged; global cross-job uniqueness is NOT guaranteed in the pilot.

Blocking policy (documented): run-scoped product failures refuse
preparation; line-scoped blocking product issues make those lines
ineligible and mark their destination BLOCKED (result: WITH_ISSUES);
warnings never block.
"""

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from apps.web.job_manager import JobError, utc_now
from apps.web.transfer import jobs
from apps.web.transfer import product_lookup as pl
from apps.web.transfer import review as review_mod
from apps.web.transfer.models import (
    JOB_PACKING_PREPARATION_COMPLETE,
    JOB_PACKING_PREPARATION_FAILED,
    JOB_PACKING_PREPARATION_IN_PROGRESS,
    JOB_PACKING_PREPARATION_WITH_ISSUES,
    JOB_PRODUCT_LOOKUP_COMPLETE,
    JOB_PRODUCT_LOOKUP_WITH_ISSUES,
)
from apps.web.transfer.review_models import REVIEW_APPROVED

PACKING_SCHEMA_VERSION = 1
RESULT_DIR = "packing"
RESULT_NAME = "result.json"

DEFAULT_CARTON_START = 1
DEFAULT_PAD_WIDTH = 3
DEFAULT_INVOICE_PREFIX = "PL"

# --- issue codes ------------------------------------------------------------------

PACKING_SOURCE_STALE = "PACKING_SOURCE_STALE"
PACKING_REVIEW_NOT_APPROVED = "PACKING_REVIEW_NOT_APPROVED"
PACKING_PRODUCT_RESULT_STALE = "PACKING_PRODUCT_RESULT_STALE"
PACKING_PRODUCT_RESULT_INVALID = "PACKING_PRODUCT_RESULT_INVALID"
PACKING_DESTINATION_MISSING = "PACKING_DESTINATION_MISSING"
PACKING_CARTON_MISSING = "PACKING_CARTON_MISSING"
PACKING_LINE_NOT_ENRICHED = "PACKING_LINE_NOT_ENRICHED"
PACKING_LINE_BLOCKED_BY_PRODUCT_ISSUE = "PACKING_LINE_BLOCKED_BY_PRODUCT_ISSUE"
PACKING_LINE_INVALID_QUANTITY = "PACKING_LINE_INVALID_QUANTITY"
PACKING_PRODUCT_IDENTITY_INCOMPLETE = "PACKING_PRODUCT_IDENTITY_INCOMPLETE"
PACKING_CONSOLIDATION_CONFLICT = "PACKING_CONSOLIDATION_CONFLICT"
PACKING_DESTINATION_BLOCKED = "PACKING_DESTINATION_BLOCKED"
PACKING_PREPARATION_FAILED_CODE = "PACKING_PREPARATION_FAILED"

SEV_BLOCKING = "blocking"
SEV_WARNING = "warning"

# Job states allowed to start/retry preparation.
PREPARABLE_STATUSES = (JOB_PRODUCT_LOOKUP_COMPLETE,
                       JOB_PRODUCT_LOOKUP_WITH_ISSUES,
                       JOB_PACKING_PREPARATION_IN_PROGRESS,
                       JOB_PACKING_PREPARATION_COMPLETE,
                       JOB_PACKING_PREPARATION_WITH_ISSUES,
                       JOB_PACKING_PREPARATION_FAILED)


# --- configuration ----------------------------------------------------------------

def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


_PREFIX_RE = re.compile(r"^[A-Z0-9][A-Z0-9-]{0,11}$")


@dataclass(frozen=True)
class PackingPreparationConfig:
    carton_start: int = DEFAULT_CARTON_START
    pad_width: int = DEFAULT_PAD_WIDTH
    invoice_prefix: str = DEFAULT_INVOICE_PREFIX

    def summary(self) -> dict:
        return {"carton_start": self.carton_start,
                "pad_width": self.pad_width,
                "invoice_prefix": self.invoice_prefix,
                "invoice_format":
                    f"{self.invoice_prefix}-<DESTINATION>-<YYYYMMDD>-<SEQ>"}


def packing_config_problems() -> list[str]:
    problems = []
    for name in ("PACKING_CARTON_START", "PACKING_CARTON_PAD_WIDTH"):
        raw = _env(name)
        if raw:
            try:
                if int(raw) < 1:
                    problems.append(f"{name} must be a positive integer.")
            except ValueError:
                problems.append(f"{name} must be a positive integer.")
    raw = _env("PACKING_INVOICE_PREFIX")
    if raw and not _PREFIX_RE.match(raw.upper()):
        problems.append("PACKING_INVOICE_PREFIX must be 1-12 characters of "
                        "A-Z, 0-9, or '-' (no path separators).")
    return problems


def load_packing_config() -> PackingPreparationConfig:
    problems = packing_config_problems()
    if problems:
        raise JobError("Packing configuration invalid: "
                       + " ".join(problems))
    return PackingPreparationConfig(
        carton_start=int(_env("PACKING_CARTON_START")
                         or DEFAULT_CARTON_START),
        pad_width=int(_env("PACKING_CARTON_PAD_WIDTH") or DEFAULT_PAD_WIDTH),
        invoice_prefix=(_env("PACKING_INVOICE_PREFIX")
                        or DEFAULT_INVOICE_PREFIX).upper(),
    )


def format_carton_number(index: int, config: PackingPreparationConfig) -> str:
    """001, 002, ... with the configured pad; grows past 999 (1000, 1001)
    without wrapping."""
    return f"{index:0{config.pad_width}d}"


# --- artifact paths + checksums ---------------------------------------------------

def result_path(job_id: str) -> Path:
    return jobs.transfer_job_dir_for(job_id) / RESULT_DIR / RESULT_NAME


def product_lookup_checksum(job_id: str) -> str | None:
    try:
        return hashlib.sha256(
            pl.result_path(job_id).read_bytes()).hexdigest()
    except (JobError, OSError):
        return None


def _current_checksums(job_id: str) -> dict:
    return {
        "extraction_checksum": review_mod.extraction_checksum(job_id),
        "review_checksum": pl._review_checksum(job_id),
        "product_lookup_checksum": product_lookup_checksum(job_id),
    }


def load_preparation(job_id: str) -> dict | None:
    """Reload the persisted preparation; adds a computed `stale` flag when
    any upstream checksum changed."""
    try:
        data = json.loads(result_path(job_id).read_text(encoding="utf-8"))
    except (JobError, OSError, ValueError):
        return None
    if not isinstance(data, dict) or "job_id" not in data:
        return None
    current = _current_checksums(job_id)
    data["stale"] = any(
        current.get(key) is None or data.get(key) != current.get(key)
        for key in ("extraction_checksum", "review_checksum",
                    "product_lookup_checksum"))
    return data


def _write_preparation(job_id: str, data: dict) -> None:
    path = result_path(job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        prior = load_preparation(job_id)
        if prior is not None and prior.get("stale"):
            stamp = utc_now().replace(":", "").replace("-", "").split(".")[0]
            target = path.with_name(f"result-stale-{stamp}.json")
            counter = 0
            while target.exists():
                counter += 1
                target = path.with_name(
                    f"result-stale-{stamp}-{counter}.json")
            os.replace(path, target)
    tmp = path.with_name(f"{RESULT_NAME}.tmp-{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# --- input boundary ---------------------------------------------------------------

def load_packing_inputs(job_id: str):
    """(job, extraction, review, enrichment) - or JobError. Stale,
    unapproved, malformed, or failed upstream data is refused; there is no
    silent fallback to raw extraction values."""
    job = jobs.load_transfer_job(job_id)
    if job is None:
        raise JobError("Unknown transfer job id.")
    if job.status not in PREPARABLE_STATUSES:
        raise JobError(f"Job in state {job.status} cannot prepare packing "
                       "groups.")
    from apps.web.transfer import extraction as extraction_mod
    result = extraction_mod.load_result(job_id)
    if result is None:
        raise JobError("Extraction result is missing.")
    review = review_mod.load_review(job_id)
    if review is None:
        raise JobError("No saved review exists.")
    if review.status != REVIEW_APPROVED:
        raise JobError("The review is not approved "
                       f"(status: {review.status}).")
    checksum = review_mod.extraction_checksum(job_id)
    if checksum is None or checksum != review.extraction_checksum:
        raise JobError("The review no longer matches the extraction "
                       "result; re-approve the review first.")
    enrichment = pl.load_enrichment(job_id)
    if enrichment is None:
        raise JobError("No product-enrichment result exists; run product "
                       "lookup first.")
    if enrichment.get("stale"):
        raise JobError("The product-enrichment result is stale (the review "
                       "changed after lookup); rerun product lookup first.")
    if enrichment.get("status") == "failed":
        raise JobError("The last product lookup failed; rerun product "
                       "lookup first.")
    return job, result, review, enrichment


# --- planning ---------------------------------------------------------------------

@dataclass
class _LineCtx:
    line: object
    enrich: dict
    product: dict | None
    destination: str | None
    destination_name: str | None
    carton_entity: object
    quantity: int | None
    order: tuple


def _line_blocking_codes(enrichment: dict) -> dict[str, list[str]]:
    """line_id -> blocking product issue codes (line-scoped)."""
    blocked: dict[str, list[str]] = {}
    for issue in enrichment.get("issues", []):
        if issue.get("severity") == pl.SEV_BLOCKING and issue.get("line_id"):
            blocked.setdefault(issue["line_id"], []).append(issue["code"])
    return blocked


def _collect_lines(review, enrichment) -> tuple[list[_LineCtx], list[dict]]:
    """Deterministic, eligibility-checked line contexts + issues for every
    ineligible line (nothing disappears silently)."""
    from apps.web.transfer import extraction as extraction_mod  # noqa: F401
    ev = review_mod.evaluate(_load_result_for(review), review)
    headers = {h.entity_id: h for h in review.headers}
    cartons = {c.entity_id: c for c in review.cartons}
    enrich_by_line = {le["line_id"]: le
                      for le in enrichment.get("line_enrichments", [])}
    products = enrichment.get("products", [])
    blocked = _line_blocking_codes(enrichment)

    issues: list[dict] = []
    contexts: list[_LineCtx] = []
    ordered = sorted(
        review.lines,
        key=lambda ln: (ln.upload_sequence, ln.source_page,
                        ln.original.get("source_sequence_number") or 0))
    for ordinal, line in enumerate(ordered):
        line_ev = ev.lines.get(line.entity_id)
        if line_ev is None or line_ev.effective_excluded:
            continue                        # excluded in review: not packed

        def issue_for(code, severity, message, destination=None):
            issues.append({
                "code": code, "severity": severity, "message": message,
                "destination": destination,
                "line_id": line.entity_id,
                "source_file": line.source_file,
                "source_page": line.source_page,
                "delivery_note_number":
                    line.original.get("delivery_note_number"),
                "original_carton_number":
                    line.original.get("original_carton_number"),
            })

        carton = cartons.get(line.carton_id)
        header = headers.get(line.document_id)
        if carton is None:
            issue_for(PACKING_CARTON_MISSING, SEV_BLOCKING,
                      "The line's source carton could not be resolved.")
            continue
        destination = pl.resolve_lookup_location(
            carton.effective("destination_code"),
            header.effective("to_location_code") if header else None)
        destination_name = (header.effective("to_location_name")
                            if header else None)
        if not destination:
            issue_for(PACKING_DESTINATION_MISSING, SEV_BLOCKING,
                      "No effective To Loc. destination for this line.")
            continue

        enrich = enrich_by_line.get(line.entity_id)
        codes = blocked.get(line.entity_id)
        if codes:
            issue_for(PACKING_LINE_BLOCKED_BY_PRODUCT_ISSUE, SEV_BLOCKING,
                      "Unresolved product issue(s): "
                      + ", ".join(sorted(set(codes))) + ".", destination)
            continue
        if enrich is None or enrich.get("status") != "matched" \
                or enrich.get("product_ref") is None:
            issue_for(PACKING_LINE_NOT_ENRICHED, SEV_BLOCKING,
                      "The line has no successful product match.",
                      destination)
            continue
        quantity = review_mod._valid_quantity(line.effective("quantity"))
        if quantity is None:
            issue_for(PACKING_LINE_INVALID_QUANTITY, SEV_BLOCKING,
                      "Effective quantity is not a positive integer.",
                      destination)
            continue
        contexts.append(_LineCtx(
            line=line, enrich=enrich,
            product=products[enrich["product_ref"]],
            destination=destination, destination_name=destination_name,
            carton_entity=carton, quantity=quantity,
            order=(line.upload_sequence, line.source_page, ordinal)))
    return contexts, issues


def _load_result_for(review):
    from apps.web.transfer import extraction as extraction_mod
    return extraction_mod.load_result(review.job_id)


# --- preparation ------------------------------------------------------------------

def _carton_sort_key(carton_entity, first_line_order):
    first_page = (carton_entity.source_pages[0]
                  if carton_entity.source_pages else 0)
    return (carton_entity.upload_sequence, first_page, first_line_order,
            carton_entity.effective("original_carton_number") or "")


def prepare_packing(job_id: str, *,
                    config: PackingPreparationConfig | None = None) -> dict:
    """Build and persist the packing preparation. Deterministic: unchanged
    inputs reproduce identical assignments, and previously issued delivery
    invoice numbers are carried forward verbatim."""
    config = config or load_packing_config()
    job, result, review, enrichment = load_packing_inputs(job_id)
    jobs.update_job_status(job_id, JOB_PACKING_PREPARATION_IN_PROGRESS)
    try:
        prepared = _build_preparation(job_id, review, enrichment, config)
    except Exception as exc:
        failed = {
            "schema_version": PACKING_SCHEMA_VERSION, "job_id": job_id,
            **_current_checksums(job_id),
            "created_at": utc_now(), "updated_at": utc_now(),
            "status": "failed", "config": config.summary(),
            "destinations": [], "issues": [{
                "code": PACKING_PREPARATION_FAILED_CODE,
                "severity": SEV_BLOCKING, "destination": None,
                "line_id": None,
                "message": f"Preparation failed ({type(exc).__name__}).",
            }], "summary": {},
        }
        _write_preparation(job_id, failed)
        jobs.update_job_status(job_id, JOB_PACKING_PREPARATION_FAILED)
        raise
    _write_preparation(job_id, prepared)
    blocking = prepared["summary"]["blocking_issues"]
    jobs.update_job_status(
        job_id, JOB_PACKING_PREPARATION_WITH_ISSUES if blocking
        else JOB_PACKING_PREPARATION_COMPLETE)
    return prepared


def _build_preparation(job_id, review, enrichment,
                       config: PackingPreparationConfig) -> dict:
    contexts, issues = _collect_lines(review, enrichment)
    if not contexts and not issues:
        raise JobError("No eligible lines exist to prepare.")
    if not contexts:
        raise JobError("No eligible enriched lines exist; resolve the "
                       "product issues first.")

    # --- destination groups in first-appearance order ---------------------------
    destinations: dict[str, dict] = {}
    for ctx in contexts:
        group = destinations.get(ctx.destination)
        if group is None:
            group = destinations[ctx.destination] = {
                "destination_code": ctx.destination,
                "destination_name": ctx.destination_name,
                "cartons": {},           # carton entity id -> line ctx list
                "carton_order": [],
                "documents": set(), "dns": set(),
            }
        if ctx.destination_name and not group["destination_name"]:
            group["destination_name"] = ctx.destination_name
        carton_id = ctx.carton_entity.entity_id
        if carton_id not in group["cartons"]:
            group["cartons"][carton_id] = []
            group["carton_order"].append(carton_id)
        group["cartons"][carton_id].append(ctx)
        group["documents"].add(ctx.line.document_id)
        dn = ctx.line.original.get("delivery_note_number")
        if dn:
            group["dns"].add(dn)

    dest_blocked = {i["destination"] for i in issues
                    if i.get("severity") == SEV_BLOCKING
                    and i.get("destination")}
    for dest in sorted(dest_blocked):
        issues.append({
            "code": PACKING_DESTINATION_BLOCKED, "severity": SEV_BLOCKING,
            "destination": dest, "line_id": None,
            "message": f"Destination {dest} has ineligible lines; its "
                       "package is incomplete until they are resolved or "
                       "excluded in review.",
        })

    # --- previous result: carry stable invoice numbers when unchanged -----------
    checksums = _current_checksums(job_id)
    prior = load_preparation(job_id)
    prior_invoices = {}
    if prior is not None and not prior.get("stale"):
        for group in prior.get("destinations", []):
            prior_invoices[group.get("destination_code")] = \
                group.get("delivery_invoice_number")
    invoice_date = None
    if prior_invoices:
        invoice_date = (prior.get("invoice_date") or None)
    invoice_date = invoice_date or datetime.now(timezone.utc).strftime(
        "%Y%m%d")

    # --- build groups ------------------------------------------------------------
    out_groups = []
    consolidation_count = 0
    total_prepared = 0
    total_units = 0
    for dest_seq, (dest, group) in enumerate(destinations.items(), start=1):
        invoice = (prior_invoices.get(dest)
                   or f"{config.invoice_prefix}-{dest}-{invoice_date}-"
                      f"{dest_seq:03d}")
        # carton order: upload sequence -> first page -> first line -> number
        ordered_cartons = sorted(
            group["carton_order"],
            key=lambda cid: _carton_sort_key(
                group["cartons"][cid][0].carton_entity,
                min(ctx.order for ctx in group["cartons"][cid])))
        carton_mappings = []
        prepared_lines = []
        for index, carton_id in enumerate(ordered_cartons):
            generated = format_carton_number(
                config.carton_start + index, config)
            ctxs = group["cartons"][carton_id]
            entity = ctxs[0].carton_entity
            carton_mappings.append({
                "destination_code": dest,
                "generated_carton_number": generated,
                "original_carton_number":
                    entity.effective("original_carton_number"),
                "extracted_carton_number":
                    entity.original.get("original_carton_number"),
                "source_carton_key": {
                    "carton_entity_id": carton_id,
                    "upload_sequence": entity.upload_sequence,
                    "source_file": entity.source_file,
                    "delivery_note_number":
                        entity.original.get("delivery_note_number"),
                    "first_source_page": (entity.source_pages[0]
                                          if entity.source_pages else None),
                },
                "sequence_index": index + 1,
                "line_count": len(ctxs),
            })
            # --- same-carton consolidation -----------------------------------
            merged: dict[tuple, dict] = {}
            order: list[tuple] = []
            for ctx in sorted(ctxs, key=lambda c: c.order):
                product = ctx.product
                identity = (product.get("item_code"),
                            product.get("color_code"),
                            product.get("size_code"))
                if not all(identity):
                    issues.append({
                        "code": PACKING_PRODUCT_IDENTITY_INCOMPLETE,
                        "severity": SEV_WARNING, "destination": dest,
                        "line_id": ctx.line.entity_id,
                        "message": "API identity fields incomplete; the "
                                   "line is kept unconsolidated.",
                    })
                    key = (generated, "UNMERGED", ctx.line.entity_id)
                else:
                    key = (dest, generated, *identity,
                           product.get("ean") or product.get("plu"))
                entry = merged.get(key)
                if entry is None:
                    entry = merged[key] = {
                        "destination_code": dest,
                        "generated_carton_number": generated,
                        "original_carton_number":
                            entity.effective("original_carton_number"),
                        "quantity": 0,
                        "source_line_ids": [],
                        "source_rows": 0,
                        "source": dict(ctx.enrich.get("source", {})),
                        "product": product,
                        "sources": [],
                    }
                    order.append(key)
                elif entry["product"] is not product:
                    issues.append({
                        "code": PACKING_CONSOLIDATION_CONFLICT,
                        "severity": SEV_BLOCKING, "destination": dest,
                        "line_id": ctx.line.entity_id,
                        "message": "Identical identity resolved to two "
                                   "different product records; lines were "
                                   "not merged.",
                    })
                    key = (generated, "CONFLICT", ctx.line.entity_id)
                    entry = merged[key] = {
                        "destination_code": dest,
                        "generated_carton_number": generated,
                        "original_carton_number":
                            entity.effective("original_carton_number"),
                        "quantity": 0, "source_line_ids": [],
                        "source_rows": 0,
                        "source": dict(ctx.enrich.get("source", {})),
                        "product": product, "sources": [],
                    }
                    order.append(key)
                entry["quantity"] += ctx.quantity
                entry["source_rows"] += 1
                entry["source_line_ids"].append(ctx.line.entity_id)
                entry["sources"].append({
                    "line_id": ctx.line.entity_id,
                    "source_file": ctx.line.source_file,
                    "upload_sequence": ctx.line.upload_sequence,
                    "source_page": ctx.line.source_page,
                    "delivery_note_number":
                        ctx.line.original.get("delivery_note_number"),
                    "original_carton_number":
                        ctx.line.original.get("original_carton_number"),
                    "source_quantity": ctx.line.original.get("quantity"),
                    "effective_quantity": ctx.quantity,
                })
            for key in order:
                entry = merged[key]
                if entry["source_rows"] > 1:
                    consolidation_count += entry["source_rows"] - 1
                prepared_lines.append(entry)
                total_units += entry["quantity"]
            total_prepared += len(order)

        dest_issue_count = lambda severity: sum(       # noqa: E731
            1 for i in issues
            if i.get("destination") == dest and i["severity"] == severity)
        out_groups.append({
            "destination_code": dest,
            "destination_name": group["destination_name"],
            "destination_sequence": dest_seq,
            "delivery_invoice_number": invoice,
            "suggested_workbook_filename":
                f"Packing_List_{dest}_{invoice}.xlsx",
            "source_document_ids": sorted(group["documents"]),
            "source_delivery_notes": sorted(group["dns"]),
            "source_carton_count": len(ordered_cartons),
            "generated_carton_count": len(ordered_cartons),
            "carton_mappings": carton_mappings,
            "prepared_lines": prepared_lines,
            "source_line_count": sum(len(v) for v in
                                     group["cartons"].values()),
            "prepared_line_count": len(prepared_lines),
            "total_units": sum(l["quantity"] for l in prepared_lines),
            "blocked": dest in dest_blocked,
            "warning_count": dest_issue_count(SEV_WARNING),
            "blocking_count": dest_issue_count(SEV_BLOCKING),
        })

    blocking_total = sum(1 for i in issues if i["severity"] == SEV_BLOCKING)
    prepared = {
        "schema_version": PACKING_SCHEMA_VERSION,
        "job_id": job_id,
        **checksums,
        "invoice_date": invoice_date,
        "created_at": (prior or {}).get("created_at") or utc_now(),
        "updated_at": utc_now(),
        "status": ("complete_with_issues" if blocking_total else "complete"),
        "config": config.summary(),
        "destinations": out_groups,
        "issues": issues,
        "summary": {
            "destinations": len(out_groups),
            "source_cartons": sum(g["source_carton_count"]
                                  for g in out_groups),
            "generated_cartons": sum(g["generated_carton_count"]
                                     for g in out_groups),
            "source_lines": sum(g["source_line_count"] for g in out_groups),
            "prepared_lines": total_prepared,
            "consolidated_rows": consolidation_count,
            "total_units": total_units,
            "blocked_destinations": sorted(dest_blocked),
            "blocking_issues": blocking_total,
            "warning_issues": sum(1 for i in issues
                                  if i["severity"] == SEV_WARNING),
        },
    }
    return prepared


# --- pre-run preview (pure; used by the UI) ---------------------------------------

def preview(job_id: str) -> dict:
    """Cheap dry-run stats for the UI. No writes, no state changes."""
    job, result, review, enrichment = load_packing_inputs(job_id)
    contexts, issues = _collect_lines(review, enrichment)
    destinations: list[str] = []
    cartons: set[str] = set()
    for ctx in contexts:
        if ctx.destination not in destinations:
            destinations.append(ctx.destination)
        cartons.add(ctx.carton_entity.entity_id)
    return {
        "destinations": destinations,
        "source_cartons": len(cartons),
        "eligible_lines": len(contexts),
        "blocked_lines": sum(1 for i in issues
                             if i["severity"] == SEV_BLOCKING
                             and i.get("line_id")),
        "total_units": sum(ctx.quantity for ctx in contexts),
        "issues": issues,
    }
