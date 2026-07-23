"""Product lookup + enrichment via /corpTool/pluLabel-get (Build 5).

Contract (ImagineX API Gateway spec v0.851-CorpTools §3.3 + the label-print
integration): POST {base}/corpTool/pluLabel-get with
{"RequestList": [{"LocationCode", "PLU", "PriceDate", "Qty"}]} and a Bearer
access token from the Build 4 auth client. Success = HTTP 2xx AND envelope
code == 100000. The gateway SKIPS non-existing PLU-location combinations -
a missing record in `data` IS the not-found signal, so responses are
correlated by the echoed (locationCode, plu), never by array position.

Identifier rules: reviewed effective EAN first (string, leading zeros
kept); fallback is the literal concatenation Item + Color + Size (a
repeated color suffix is never removed - the spec's own PLU example
CM0010007804M5C2WAHM is exactly itemCode+colorCode+sizeCode). Duplicate
API lookups are eliminated per (location, price date, PLU); source lines
are NEVER merged and quantities are never changed.

Policies isolated for business confirmation (documented in FUNCTIONAL_SPEC):
resolve_lookup_location() -> effective destination To Loc. code;
resolve_price_date() -> effective delivery-note date, else the
PRODUCT_LOOKUP_PRICE_DATE override, else a blocking readiness problem;
resolve_lookup_qty() -> constant 1 (lookup-only semantics).

Security: composes the Build 4 client - tokens stay in its process cache;
this module never logs, persists, or returns tokens or Authorization
headers, and the persisted artifact is token-free by construction (tested).
"""

import json
import os
import re
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path

from apps.web.job_manager import JobError, utc_now
from apps.web.transfer import extraction as extraction_mod
from apps.web.transfer import jobs
from apps.web.transfer import review as review_mod
from apps.web.transfer.gateway_auth import (
    ApiGatewayAuthClient,
    AuthError,
    TransportFailure,
    TransportTimeout,
    build_client,
    config_problems as auth_config_problems,
    logger,
    redact,
)
from apps.web.transfer.models import (
    JOB_PRODUCT_LOOKUP_COMPLETE,
    JOB_PRODUCT_LOOKUP_FAILED,
    JOB_PRODUCT_LOOKUP_IN_PROGRESS,
    JOB_PRODUCT_LOOKUP_WITH_ISSUES,
    JOB_READY_FOR_PRODUCT_LOOKUP,
)
from apps.web.transfer.review_models import REVIEW_APPROVED

ENRICHMENT_SCHEMA_VERSION = 1
RESULT_DIR = "product_lookup"
RESULT_NAME = "result.json"

DEFAULT_LOOKUP_PATH = "/corpTool/pluLabel-get"
DEFAULT_BATCH_SIZE = 50
DEFAULT_TIMEOUT_SECONDS = 120
DEFAULT_MAX_RETRIES = 3
LOOKUP_QTY = 1     # lookup-only semantics; see resolve_lookup_qty()

IDENTIFIER_EAN = "EAN"
IDENTIFIER_CONSTRUCTED = "CONSTRUCTED"

# --- typed client error codes -----------------------------------------------------

PRODUCT_CONFIGURATION_ERROR = "PRODUCT_CONFIGURATION_ERROR"
PRODUCT_TRANSPORT_ERROR = "PRODUCT_TRANSPORT_ERROR"
PRODUCT_TIMEOUT = "PRODUCT_TIMEOUT"
PRODUCT_HTTP_ERROR = "PRODUCT_HTTP_ERROR"
PRODUCT_GATEWAY_REJECTED = "PRODUCT_GATEWAY_REJECTED"
PRODUCT_AUTH_ERROR = "PRODUCT_AUTH_ERROR"
PRODUCT_ACCESS_DENIED = "PRODUCT_ACCESS_DENIED"
PRODUCT_RESPONSE_INVALID = "PRODUCT_RESPONSE_INVALID"
PRODUCT_CORRELATION_ERROR = "PRODUCT_CORRELATION_ERROR"
PRODUCT_RETRY_EXHAUSTED = "PRODUCT_RETRY_EXHAUSTED"

# --- enrichment issue codes -------------------------------------------------------

PRODUCT_NOT_FOUND = "PRODUCT_NOT_FOUND"
PRODUCT_MULTIPLE_MATCHES = "PRODUCT_MULTIPLE_MATCHES"
PRODUCT_LOOKUP_API_ERROR = "PRODUCT_LOOKUP_API_ERROR"
PRODUCT_LOOKUP_AUTH_ERROR = "PRODUCT_LOOKUP_AUTH_ERROR"
PRODUCT_LOOKUP_ACCESS_DENIED = "PRODUCT_LOOKUP_ACCESS_DENIED"
PRODUCT_LOOKUP_RESPONSE_INVALID = "PRODUCT_LOOKUP_RESPONSE_INVALID"
PRODUCT_LOOKUP_RESPONSE_AMBIGUOUS = "PRODUCT_LOOKUP_RESPONSE_AMBIGUOUS"
PRODUCT_LOOKUP_IDENTIFIER_MISSING = "PRODUCT_LOOKUP_IDENTIFIER_MISSING"
PRODUCT_EAN_MISMATCH = "PRODUCT_EAN_MISMATCH"
PRODUCT_ITEM_MISMATCH = "PRODUCT_ITEM_MISMATCH"
PRODUCT_COLOR_MISMATCH = "PRODUCT_COLOR_MISMATCH"
PRODUCT_SIZE_MISMATCH = "PRODUCT_SIZE_MISMATCH"
PRODUCT_DESCRIPTION_MISMATCH = "PRODUCT_DESCRIPTION_MISMATCH"
PRODUCT_RETAIL_PRICE_MISMATCH = "PRODUCT_RETAIL_PRICE_MISMATCH"

SEV_BLOCKING = "blocking"
SEV_WARNING = "warning"


class ProductError(Exception):
    """Typed, redaction-safe product-lookup error (client level)."""

    def __init__(self, code: str, message: str, *, operation: str = "lookup",
                 batch_number: int | None = None,
                 request_count: int | None = None,
                 http_status: int | None = None,
                 gateway_code: int | None = None,
                 retryable: bool = False):
        self.code = code
        self.operation = operation
        self.batch_number = batch_number
        self.request_count = request_count
        self.http_status = http_status
        self.gateway_code = gateway_code
        self.retryable = retryable
        tail = [f"operation={operation}"]
        if batch_number is not None:
            tail.append(f"batch={batch_number}")
        if http_status is not None:
            tail.append(f"http={http_status}")
        if gateway_code is not None:
            tail.append(f"gateway_code={gateway_code}")
        super().__init__(f"[{code}] {message} ({', '.join(tail)})")


# --- configuration ----------------------------------------------------------------

def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


@dataclass(frozen=True)
class ProductLookupConfig:
    lookup_path: str = DEFAULT_LOOKUP_PATH
    batch_size: int = DEFAULT_BATCH_SIZE
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_MAX_RETRIES
    price_date_override: str | None = None

    def summary(self) -> dict:
        """Secret-free configuration summary for the persisted artifact."""
        return {"lookup_path": self.lookup_path,
                "batch_size": self.batch_size,
                "timeout_seconds": self.timeout_seconds,
                "max_retries": self.max_retries,
                "qty_policy": f"constant {LOOKUP_QTY} (lookup-only)",
                "price_date_override": self.price_date_override}


def product_config_problems() -> list[str]:
    problems = []
    path = _env("PRODUCT_LOOKUP_PATH")
    if path and not path.startswith("/"):
        problems.append("PRODUCT_LOOKUP_PATH must start with '/'.")
    for name, minimum in (("PRODUCT_LOOKUP_BATCH_SIZE", 1),
                          ("PRODUCT_LOOKUP_MAX_RETRIES", 0)):
        raw = _env(name)
        if raw:
            try:
                if int(raw) < minimum:
                    problems.append(f"{name} must be >= {minimum}.")
            except ValueError:
                problems.append(f"{name} must be an integer.")
    raw = _env("PRODUCT_LOOKUP_TIMEOUT_SECONDS")
    if raw:
        try:
            if float(raw) <= 0:
                problems.append("PRODUCT_LOOKUP_TIMEOUT_SECONDS must be > 0.")
        except ValueError:
            problems.append("PRODUCT_LOOKUP_TIMEOUT_SECONDS must be a number.")
    raw = _env("PRODUCT_LOOKUP_PRICE_DATE")
    if raw and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        problems.append("PRODUCT_LOOKUP_PRICE_DATE must be YYYY-MM-DD.")
    return problems


def load_product_config() -> ProductLookupConfig:
    problems = product_config_problems()
    if problems:
        raise ProductError(PRODUCT_CONFIGURATION_ERROR, " ".join(problems),
                           operation="configuration")
    retries = int(_env("PRODUCT_LOOKUP_MAX_RETRIES") or DEFAULT_MAX_RETRIES)
    return ProductLookupConfig(
        lookup_path=_env("PRODUCT_LOOKUP_PATH") or DEFAULT_LOOKUP_PATH,
        batch_size=max(1, int(_env("PRODUCT_LOOKUP_BATCH_SIZE")
                              or DEFAULT_BATCH_SIZE)),
        timeout_seconds=float(_env("PRODUCT_LOOKUP_TIMEOUT_SECONDS")
                              or DEFAULT_TIMEOUT_SECONDS),
        max_retries=min(max(retries, 0), 10),
        price_date_override=_env("PRODUCT_LOOKUP_PRICE_DATE") or None,
    )


def readiness() -> dict:
    """Config-only readiness (auth + product settings). No network call;
    variable names only, never values."""
    problems = auth_config_problems() + product_config_problems()
    if not problems:
        return {"status": "configured", "problems": []}
    only_unset = all("is not set" in p for p in problems)
    return {"status": "not_configured" if only_unset
            else "configuration_error", "problems": problems}


# --- policies (isolated; see module docstring) ------------------------------------

def resolve_lookup_location(line_dest: str | None,
                            header_dest: str | None) -> str | None:
    """LocationCode = the effective destination (To Loc.) code: the packing
    list needs the destination's price list. Policy function - change here,
    not inline."""
    dest = (line_dest or header_dest or "").strip().upper()
    return dest or None


def resolve_price_date(header_date: str | None,
                       override: str | None) -> str | None:
    """PriceDate = effective delivery-note date (ISO). An explicit
    PRODUCT_LOOKUP_PRICE_DATE override wins; otherwise a missing/invalid
    document date is a blocking planning problem - today's date is NEVER
    silently used."""
    if override:
        return override
    date = (header_date or "").strip()
    return date if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) else None


def resolve_lookup_qty() -> int:
    """Qty = 1 for every lookup request (lookup-only semantics). The
    label-print system sends line quantities and the spec hints Qty may
    influence returned prices - business confirmation is required before
    changing this. A constant makes deduplication and correlation exact."""
    return LOOKUP_QTY


# --- identifier construction ------------------------------------------------------

def _norm_text(value) -> str:
    return re.sub(r"\s+", " ",
                  unicodedata.normalize("NFKC", str(value or ""))).strip()


def build_identifiers(line) -> tuple[str | None, str | None]:
    """(ean_identifier, constructed_identifier) from a reviewed line's
    effective values. EAN stays a string with leading zeros; the fallback
    is the LITERAL concatenation item+color+size - a repeated color suffix
    inside the item code is deliberately NOT removed."""
    ean = _norm_text(line.effective("ean")).replace(" ", "")
    ean_id = ean if review_mod.valid_ean(ean) else None
    item = _norm_text(line.effective("item_code")).upper()
    color = _norm_text(line.effective("color_code")).upper()
    size = _norm_text(line.effective("size_code")).upper()
    constructed = (item + color + size) if (item and color and size) else None
    return ean_id, constructed


# --- planning models --------------------------------------------------------------

@dataclass(frozen=True)
class ProductLookupKey:
    location_code: str
    price_date: str
    plu: str
    identifier_type: str          # EAN | CONSTRUCTED

    def request_item(self) -> dict:
        # exact confirmed request casing and types
        return {"LocationCode": self.location_code, "PLU": self.plu,
                "PriceDate": self.price_date, "Qty": resolve_lookup_qty()}

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class PlannedLookup:
    key: ProductLookupKey
    line_ids: list[str] = field(default_factory=list)
    first_seen_sequence: int = 0        # deterministic audit order
    fallback: ProductLookupKey | None = None

    def as_dict(self) -> dict:
        return {"key": self.key.as_dict(),
                "line_ids": list(self.line_ids),
                "first_seen_sequence": self.first_seen_sequence,
                "fallback": self.fallback.as_dict() if self.fallback else None}


@dataclass
class LookupPlan:
    lookups: list[PlannedLookup] = field(default_factory=list)
    line_issues: list[dict] = field(default_factory=list)  # planning issues
    line_count: int = 0
    ean_lines: int = 0
    fallback_ready_lines: int = 0
    no_identifier_lines: int = 0
    locations: list[str] = field(default_factory=list)
    price_dates: list[str] = field(default_factory=list)
    planning_problems: list[str] = field(default_factory=list)


def load_approved_inputs(job_id: str):
    """The ONLY input path: current extraction + APPROVED, non-stale review
    + an allowed job state. Raises JobError otherwise."""
    job = jobs.load_transfer_job(job_id)
    if job is None:
        raise JobError("Unknown transfer job id.")
    allowed = (JOB_READY_FOR_PRODUCT_LOOKUP, JOB_PRODUCT_LOOKUP_IN_PROGRESS,
               JOB_PRODUCT_LOOKUP_WITH_ISSUES, JOB_PRODUCT_LOOKUP_FAILED,
               JOB_PRODUCT_LOOKUP_COMPLETE)
    if job.status not in allowed:
        raise JobError(f"Job in state {job.status} cannot run product "
                       "lookup.")
    result = extraction_mod.load_result(job_id)
    if result is None:
        raise JobError("Extraction result is missing.")
    review = review_mod.load_review(job_id)
    if review is None:
        raise JobError("No saved review exists.")
    if review.status != REVIEW_APPROVED:
        raise JobError("The review is not approved (status: "
                       f"{review.status}); product lookup requires an "
                       "approved, current review.")
    checksum = review_mod.extraction_checksum(job_id)
    if checksum is None or checksum != review.extraction_checksum:
        raise JobError("The review no longer matches the extraction result; "
                       "rebuild and re-approve the review first.")
    return job, result, review


def build_plan(job_id: str,
               config: ProductLookupConfig | None = None) -> LookupPlan:
    """Deterministic lookup plan from ONLY the approved review's effective
    included lines. Dedupes identical (location, date, PLU) requests while
    keeping every source line separate."""
    config = config or load_product_config()
    _, result, review = load_approved_inputs(job_id)
    ev = review_mod.evaluate(result, review)
    headers = {h.entity_id: h for h in review.headers}
    cartons = {c.entity_id: c for c in review.cartons}

    plan = LookupPlan()
    by_key: dict[ProductLookupKey, PlannedLookup] = {}
    ordered = sorted(
        review.lines,
        key=lambda ln: (ln.upload_sequence, ln.source_page,
                        ln.original.get("source_sequence_number") or 0))
    sequence = 0
    locations: set[str] = set()
    dates: set[str] = set()
    for line in ordered:
        line_ev = ev.lines.get(line.entity_id)
        if line_ev is None or line_ev.effective_excluded:
            continue
        plan.line_count += 1
        sequence += 1
        header = headers.get(line.document_id)
        carton = cartons.get(line.carton_id)
        location = resolve_lookup_location(
            carton.effective("destination_code") if carton else None,
            header.effective("to_location_code") if header else None)
        price_date = resolve_price_date(
            header.effective("delivery_date") if header else None,
            config.price_date_override)
        if location is None:
            plan.planning_problems.append(
                f"{line.entity_id}: no destination code available.")
            continue
        if price_date is None:
            plan.planning_problems.append(
                f"{line.entity_id}: no valid delivery-note date for "
                "PriceDate (set PRODUCT_LOOKUP_PRICE_DATE to override).")
            continue
        locations.add(location)
        dates.add(price_date)
        ean_id, constructed = build_identifiers(line)
        if ean_id:
            plan.ean_lines += 1
        if constructed:
            plan.fallback_ready_lines += 1
        if not ean_id and not constructed:
            plan.no_identifier_lines += 1
            plan.line_issues.append({
                "code": PRODUCT_LOOKUP_IDENTIFIER_MISSING,
                "severity": SEV_BLOCKING,
                "line_id": line.entity_id,
                "message": "Neither a valid EAN nor Item+Color+Size is "
                           "available; the API is not called for this line.",
            })
            continue
        primary_type = IDENTIFIER_EAN if ean_id else IDENTIFIER_CONSTRUCTED
        primary_value = ean_id or constructed
        key = ProductLookupKey(location_code=location,
                               price_date=price_date, plu=primary_value,
                               identifier_type=primary_type)
        fallback = None
        if ean_id and constructed:
            fallback = ProductLookupKey(location_code=location,
                                        price_date=price_date,
                                        plu=constructed,
                                        identifier_type=IDENTIFIER_CONSTRUCTED)
        planned = by_key.get(key)
        if planned is None:
            planned = PlannedLookup(key=key, first_seen_sequence=sequence,
                                    fallback=fallback)
            by_key[key] = planned
            plan.lookups.append(planned)     # insertion = deterministic order
        elif planned.fallback is None and fallback is not None:
            planned.fallback = fallback
        planned.line_ids.append(line.entity_id)
    plan.locations = sorted(locations)
    plan.price_dates = sorted(dates)
    return plan


def make_batches(lookups: list[ProductLookupKey],
                 batch_size: int) -> list[list[ProductLookupKey]]:
    if batch_size < 1:
        raise ProductError(PRODUCT_CONFIGURATION_ERROR,
                           "Batch size must be positive.",
                           operation="configuration")
    return [lookups[i:i + batch_size]
            for i in range(0, len(lookups), batch_size)]


# --- transport + authenticated client ---------------------------------------------

class ProductTransport:
    """Injectable HTTP transport (httpx in production; fakes in tests)."""

    def post_json(self, url: str, body: dict, *, headers: dict,
                  timeout: float):
        import httpx
        try:
            response = httpx.post(url, json=body, timeout=timeout,
                                  headers={"Content-Type": "application/json",
                                           **headers})
        except httpx.TimeoutException as exc:
            raise TransportTimeout(type(exc).__name__) from exc
        except httpx.HTTPError as exc:
            raise TransportFailure(type(exc).__name__) from exc
        try:
            parsed = response.json()
        except (json.JSONDecodeError, ValueError):
            parsed = None
        return response.status_code, parsed


@dataclass
class BatchOutcome:
    batch_number: int
    request_count: int
    http_status: int | None
    gateway_code: int | None
    records: list[dict]
    duration_seconds: float
    auth_recovered: bool = False


class ProductGatewayClient:
    """Authenticated pluLabel-get caller composing the Build 4 auth client.
    One 401 recovery (handle_unauthorized) per batch, then one retry of
    THAT batch; narrow transient retries; never exposes tokens."""

    def __init__(self, auth: ApiGatewayAuthClient,
                 config: ProductLookupConfig,
                 transport: ProductTransport | None = None,
                 clock=time.time, sleep=time.sleep):
        self.auth = auth
        self.config = config
        self.transport = transport or ProductTransport()
        self.clock = clock
        self.sleep = sleep

    @property
    def lookup_url(self) -> str:
        return self.auth.config.base_url + self.config.lookup_path

    def lookup_batch(self, batch: list[ProductLookupKey],
                     batch_number: int) -> BatchOutcome:
        body = {"RequestList": [k.request_item() for k in batch]}
        started = self.clock()
        try:
            token = self.auth.ensure_access_token()
        except AuthError as exc:
            raise ProductError(PRODUCT_AUTH_ERROR,
                               f"Authentication failed: {exc.code}.",
                               batch_number=batch_number,
                               request_count=len(batch)) from exc
        status, parsed = self._send_with_retries(body, token, batch_number,
                                                 len(batch))
        auth_recovered = False
        if status == 401:
            logger.info("product lookup batch %d unauthorized; recovering "
                        "via auth client and retrying once", batch_number)
            try:
                token = self.auth.handle_unauthorized()
            except AuthError as exc:
                raise ProductError(PRODUCT_AUTH_ERROR,
                                   "Authentication could not be restored: "
                                   f"{exc.code}.", batch_number=batch_number,
                                   request_count=len(batch)) from exc
            status, parsed = self._send_with_retries(body, token,
                                                     batch_number, len(batch))
            auth_recovered = True
            if status == 401:
                raise ProductError(PRODUCT_ACCESS_DENIED,
                                   "Lookup remained unauthorized after "
                                   "authentication recovery.",
                                   batch_number=batch_number,
                                   request_count=len(batch), http_status=401)
        return self._interpret(status, parsed, batch_number, len(batch),
                               self.clock() - started, auth_recovered)

    def _send_with_retries(self, body, token, batch_number, request_count):
        attempts = max(0, self.config.max_retries) + 1
        last: ProductError | None = None
        for attempt in range(1, attempts + 1):
            try:
                return self.transport.post_json(
                    self.lookup_url, body,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=self.config.timeout_seconds)
            except TransportTimeout:
                last = ProductError(PRODUCT_TIMEOUT,
                                    "The product API did not respond in "
                                    f"{self.config.timeout_seconds:.0f}s.",
                                    batch_number=batch_number,
                                    request_count=request_count,
                                    retryable=True)
            except TransportFailure:
                last = ProductError(PRODUCT_TRANSPORT_ERROR,
                                    "The product API could not be reached.",
                                    batch_number=batch_number,
                                    request_count=request_count,
                                    retryable=True)
            logger.warning("product batch %d attempt %d/%d failed: %s",
                           batch_number, attempt, attempts, last.code)
            if attempt < attempts:
                self.sleep(min(2.0 ** (attempt - 1), 5.0))
        raise ProductError(PRODUCT_RETRY_EXHAUSTED,
                           f"Batch failed after {attempts} attempt(s): "
                           f"{last.code}.", batch_number=batch_number,
                           request_count=request_count)

    def _interpret(self, status, parsed, batch_number, request_count,
                   duration, auth_recovered) -> BatchOutcome:
        if status >= 500:
            raise ProductError(PRODUCT_HTTP_ERROR,
                               "The product API returned a server error.",
                               batch_number=batch_number,
                               request_count=request_count,
                               http_status=status, retryable=True)
        if not (200 <= status < 300):
            raise ProductError(PRODUCT_GATEWAY_REJECTED,
                               "The product API rejected the request.",
                               batch_number=batch_number,
                               request_count=request_count,
                               http_status=status)
        if not isinstance(parsed, dict):
            raise ProductError(PRODUCT_RESPONSE_INVALID,
                               "The product API response was not a JSON "
                               "envelope.", batch_number=batch_number,
                               request_count=request_count,
                               http_status=status)
        code = parsed.get("code")
        try:
            code = int(code) if code is not None else None
        except (TypeError, ValueError):
            code = None
        if code != self.auth.config.success_code:
            raise ProductError(PRODUCT_GATEWAY_REJECTED,
                               "The product API reported a failed "
                               "operation.", batch_number=batch_number,
                               request_count=request_count,
                               http_status=status, gateway_code=code)
        data = parsed.get("data")
        records = [r for r in data if isinstance(r, dict)] \
            if isinstance(data, list) else []
        if data is not None and not isinstance(data, list):
            raise ProductError(PRODUCT_RESPONSE_INVALID,
                               "The product API data payload was not a "
                               "list.", batch_number=batch_number,
                               request_count=request_count,
                               http_status=status, gateway_code=code)
        return BatchOutcome(batch_number=batch_number,
                            request_count=request_count, http_status=status,
                            gateway_code=code, records=records,
                            duration_seconds=round(duration, 3),
                            auth_recovered=auth_recovered)


# --- response normalization -------------------------------------------------------

_ANALYSIS_RE = re.compile(r"(?i)^analysis[_ ]?code[_ ]?0?(\d{1,2})$")
_COMPOSITION_RE = re.compile(r"(?i)^composition[_ #]?0?(\d{1,2})$")


def _wire(raw: dict, *names):
    """First present, non-empty wire value among camelCase/PascalCase names;
    numbers are stringified (prices arrive as JSON numbers)."""
    for name in names:
        for candidate in (name, name[0].upper() + name[1:]):
            if candidate in raw:
                value = raw[candidate]
                if isinstance(value, (int, float)):
                    return str(value)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return None


def normalize_record(raw: dict) -> dict:
    """Wire record -> normalized product dict. Analysis Code 01-15 and
    Composition #1-4 wire names are NOT present in any local specification
    (only xf_group5/12/16 appear in evidence); this adapter captures any
    key matching the documented patterns and keeps the full token-free raw
    record so nothing is lost whatever the live gateway returns."""
    product = {
        "org_id": _wire(raw, "orgId"),
        "location_code": _wire(raw, "locationCode"),
        "brand": _wire(raw, "brand"),
        "brand_name": _wire(raw, "brandName"),
        "currency": _wire(raw, "currency"),
        "item_code": _wire(raw, "itemCode"),
        "style": _wire(raw, "style", "styleCode"),
        "color_code": _wire(raw, "colorCode"),
        "color_desc": _wire(raw, "colorDesc"),
        "size_code": _wire(raw, "sizeCode"),
        "plu": _wire(raw, "plu", "pLU", "PLU"),
        "ean": _wire(raw, "ean", "eAN", "EAN"),
        "item_desc": _wire(raw, "itemDesc"),
        "long_item_desc": _wire(raw, "longItemDesc"),
        "season": _wire(raw, "season"),
        "subcat": _wire(raw, "subcat"),
        "gender": _wire(raw, "gender"),
        "prod_line": _wire(raw, "prodLine"),
        "supplier_item_code": _wire(raw, "supplierItemCode"),
        "original_retail_price": _wire(raw, "originalRetailPrice"),
        "discount_price": _wire(raw, "discountPrice"),
        "qty_echo": _wire(raw, "qty"),
    }
    for i in range(1, 16):
        product[f"analysis_code_{i:02d}"] = None
    for i in range(1, 5):
        product[f"composition_{i:02d}"] = None
    xf_groups = {}
    for key, value in raw.items():
        text = (str(value).strip()
                if isinstance(value, (str, int, float)) else None)
        m = _ANALYSIS_RE.match(str(key))
        if m and 1 <= int(m.group(1)) <= 15:
            product[f"analysis_code_{int(m.group(1)):02d}"] = text
            continue
        m = _COMPOSITION_RE.match(str(key))
        if m and 1 <= int(m.group(1)) <= 4:
            product[f"composition_{int(m.group(1)):02d}"] = text
            continue
        if str(key).lower().startswith("xf_group"):
            xf_groups[str(key).lower()] = text
    product["xf_groups"] = xf_groups
    product["raw"] = redact(raw)          # token-free by construction
    return product


# --- comparison -------------------------------------------------------------------

def _cmp_norm(value) -> str:
    return _norm_text(value).upper()


def compare_line(line, product: dict, identifier_type: str) -> list[dict]:
    """Source (reviewed effective) vs API values. Identity mismatches are
    blocking; wording/price differences and API-resolved gaps are warnings.
    Reviewed values are never modified."""
    issues = []

    def issue(code, severity, field_name, source, api):
        issues.append({"code": code, "severity": severity,
                       "line_id": line.entity_id, "field": field_name,
                       "source_value": source, "api_value": api,
                       "message": f"{field_name}: source "
                                  f"'{source or ''}' vs API '{api or ''}'."})

    pairs = (("item_code", "item_code", PRODUCT_ITEM_MISMATCH, SEV_BLOCKING),
             ("color_code", "color_code", PRODUCT_COLOR_MISMATCH,
              SEV_BLOCKING),
             ("size_code", "size_code", PRODUCT_SIZE_MISMATCH, SEV_BLOCKING))
    for src_field, api_field, code, severity in pairs:
        source = _cmp_norm(line.effective(src_field))
        api = _cmp_norm(product.get(api_field))
        if source and api and source != api:
            issue(code, severity, src_field, line.effective(src_field),
                  product.get(api_field))

    source_ean = _norm_text(line.effective("ean")).replace(" ", "")
    api_ean = _norm_text(product.get("ean")).replace(" ", "")
    if source_ean and api_ean and source_ean != api_ean:
        # documented policy: when the CONSTRUCTED fallback matched, the API
        # EAN is authoritative and the difference is a warning; an EAN-keyed
        # match returning a different EAN is an identity problem.
        severity = (SEV_WARNING if identifier_type == IDENTIFIER_CONSTRUCTED
                    else SEV_BLOCKING)
        issue(PRODUCT_EAN_MISMATCH, severity, "ean", source_ean, api_ean)

    source_desc = _cmp_norm(line.effective("description"))
    api_desc = _cmp_norm(product.get("item_desc")
                         or product.get("long_item_desc"))
    if source_desc and api_desc and source_desc != api_desc:
        issue(PRODUCT_DESCRIPTION_MISMATCH, SEV_WARNING, "description",
              line.effective("description"), product.get("item_desc"))

    source_price = _norm_text(line.effective("retail_price"))
    api_price = _norm_text(product.get("original_retail_price"))
    if source_price and api_price:
        try:
            from decimal import Decimal
            if Decimal(source_price) != Decimal(api_price):
                issue(PRODUCT_RETAIL_PRICE_MISMATCH, SEV_WARNING,
                      "retail_price", source_price, api_price)
        except Exception:
            issue(PRODUCT_RETAIL_PRICE_MISMATCH, SEV_WARNING,
                  "retail_price", source_price, api_price)
    return issues


# --- persisted enrichment artifact ------------------------------------------------

def result_path(job_id: str) -> Path:
    return jobs.transfer_job_dir_for(job_id) / RESULT_DIR / RESULT_NAME


def load_enrichment(job_id: str) -> dict | None:
    """Reload the persisted enrichment (refresh recovery). Marks the
    artifact stale in-memory when the review checksum no longer matches."""
    try:
        path = result_path(job_id)
        data = json.loads(path.read_text(encoding="utf-8"))
    except (JobError, OSError, ValueError):
        return None
    if not isinstance(data, dict) or "job_id" not in data:
        return None
    review = review_mod.load_review(job_id, check_stale=False)
    if review is None or review.extraction_checksum is None:
        data["stale"] = True
    else:
        current = _review_checksum(job_id)
        data["stale"] = (current is None
                         or data.get("review_checksum") != current)
    return data


def _review_checksum(job_id: str) -> str | None:
    """Checksum of the saved review artifact bytes - re-approving or
    editing the review changes it, which makes enrichment stale."""
    import hashlib
    try:
        path = review_mod.review_path(job_id)
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except (JobError, OSError):
        return None


def _write_enrichment(job_id: str, data: dict) -> None:
    path = result_path(job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        prior = load_enrichment(job_id)
        if prior is not None and prior.get("stale"):
            # the source review changed since the old result: archive it
            stamp = utc_now().replace(":", "").replace("-", "").split(".")[0]
            target = path.with_name(f"result-stale-{stamp}.json")
            counter = 0
            while target.exists():
                counter += 1
                target = path.with_name(f"result-stale-{stamp}-{counter}.json")
            os.replace(path, target)
    tmp = path.with_name(f"{RESULT_NAME}.tmp-{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# --- correlation ------------------------------------------------------------------

def correlate_records(batch: list[ProductLookupKey],
                      records: list[dict]) -> tuple[dict, list[dict]]:
    """Map each requested key to its normalized product record using the
    echoed (locationCode, plu) - NEVER array position. A requested EAN may
    also match a record's authoritative `ean` field. Returns
    (key->product | list, batch_issues)."""
    issues = []
    matches: dict[ProductLookupKey, list[dict]] = {k: [] for k in batch}
    claimed = [False] * len(records)
    normalized = [normalize_record(r) for r in records]
    for key in batch:
        for index, product in enumerate(normalized):
            loc = _cmp_norm(product.get("location_code"))
            if loc and loc != _cmp_norm(key.location_code):
                continue
            plu = _cmp_norm(product.get("plu"))
            ean = _cmp_norm(product.get("ean"))
            wanted = _cmp_norm(key.plu)
            if wanted and (plu == wanted or ean == wanted):
                matches[key].append(product)
                claimed[index] = True
    unclaimed = [normalized[i] for i, used in enumerate(claimed) if not used]
    for product in unclaimed:
        issues.append({"code": PRODUCT_LOOKUP_RESPONSE_AMBIGUOUS,
                       "severity": SEV_BLOCKING,
                       "message": "The API returned a record that matches "
                                  "no request in the batch (echoed "
                                  f"plu '{product.get('plu') or '?'}').",
                       "line_id": None, "field": None,
                       "source_value": None,
                       "api_value": product.get("plu")})
    return matches, issues


# --- the enrichment run -----------------------------------------------------------

def run_product_lookup(job_id: str, *,
                       auth: ApiGatewayAuthClient | None = None,
                       transport: ProductTransport | None = None,
                       config: ProductLookupConfig | None = None,
                       on_progress=None) -> dict:
    """Execute the full Build 5 enrichment for an approved job. Stops
    before grouping, carton renumbering, row consolidation, or any
    workbook output."""
    config = config or load_product_config()
    job, result, review = load_approved_inputs(job_id)
    plan = build_plan(job_id, config)
    if plan.planning_problems:
        raise JobError("Product lookup cannot start: "
                       + " ".join(plan.planning_problems[:3]))
    jobs.update_job_status(job_id, JOB_PRODUCT_LOOKUP_IN_PROGRESS)

    enrichment = {
        "schema_version": ENRICHMENT_SCHEMA_VERSION,
        "job_id": job_id,
        "review_checksum": _review_checksum(job_id),
        "created_at": utc_now(),
        "updated_at": "",
        "status": "in_progress",
        "config": config.summary(),
        "plan": {
            "line_count": plan.line_count,
            "unique_lookups": len(plan.lookups),
            "ean_lines": plan.ean_lines,
            "fallback_ready_lines": plan.fallback_ready_lines,
            "no_identifier_lines": plan.no_identifier_lines,
            "locations": plan.locations,
            "price_dates": plan.price_dates,
        },
        "lookups": [p.as_dict() for p in plan.lookups],
        "batches": [],
        "products": [],
        "line_enrichments": [],
        "issues": list(plan.line_issues),
        "summary": {},
    }

    try:
        if auth is None:
            auth = build_client()
        client = ProductGatewayClient(auth, config, transport=transport)
        key_results = _run_stages(client, plan, config, enrichment,
                                  on_progress)
        _assemble_line_enrichments(review, plan, key_results, enrichment)
    except (ProductError, AuthError, JobError) as exc:
        code = getattr(exc, "code", None)
        enrichment["issues"].append({
            "code": (PRODUCT_LOOKUP_AUTH_ERROR
                     if isinstance(exc, AuthError)
                     or code in (PRODUCT_AUTH_ERROR,)
                     else PRODUCT_LOOKUP_ACCESS_DENIED
                     if code == PRODUCT_ACCESS_DENIED
                     else PRODUCT_LOOKUP_API_ERROR),
            "severity": SEV_BLOCKING, "line_id": None, "field": None,
            "source_value": None, "api_value": None,
            "message": str(exc)})
        enrichment["status"] = "failed"
        enrichment["updated_at"] = utc_now()
        _summarize(enrichment)
        _write_enrichment(job_id, enrichment)
        jobs.update_job_status(job_id, JOB_PRODUCT_LOOKUP_FAILED)
        return enrichment

    blocking = sum(1 for i in enrichment["issues"]
                   if i.get("severity") == SEV_BLOCKING)
    enrichment["status"] = "complete_with_issues" if blocking else "complete"
    enrichment["updated_at"] = utc_now()
    _summarize(enrichment)
    _write_enrichment(job_id, enrichment)
    jobs.update_job_status(job_id,
                           JOB_PRODUCT_LOOKUP_WITH_ISSUES if blocking
                           else JOB_PRODUCT_LOOKUP_COMPLETE)
    return enrichment


def _run_stages(client, plan, config, enrichment, on_progress):
    """Stage 1: all primary lookups (batched). Stage 2: deduplicated
    constructed fallbacks for definitive not-founds only. Returns
    key -> {"product": dict|None, "products": [..], "stage": ...}."""
    key_results: dict[ProductLookupKey, dict] = {}

    def run_batches(keys: list[ProductLookupKey], stage: str,
                    batch_offset: int) -> int:
        batches = make_batches(keys, config.batch_size)
        for number, batch in enumerate(batches, start=batch_offset + 1):
            if on_progress is not None:
                try:
                    on_progress(stage, number,
                                batch_offset + len(batches), len(batch))
                except Exception:
                    pass
            outcome = client.lookup_batch(batch, number)
            matches, batch_issues = correlate_records(batch, outcome.records)
            enrichment["issues"].extend(batch_issues)
            enrichment["batches"].append({
                "batch_number": number, "stage": stage,
                "request_count": outcome.request_count,
                "http_status": outcome.http_status,
                "gateway_code": outcome.gateway_code,
                "records_returned": len(outcome.records),
                "duration_seconds": outcome.duration_seconds,
                "auth_recovered": outcome.auth_recovered,
            })
            for key in batch:
                found = matches.get(key, [])
                key_results[key] = {"stage": stage, "products": found}
        return len(batches)

    primary_keys = [p.key for p in plan.lookups]
    used = run_batches(primary_keys, "primary", 0)

    fallback_keys: list[ProductLookupKey] = []
    seen: set[ProductLookupKey] = set()
    for planned in plan.lookups:
        outcome = key_results.get(planned.key)
        if (outcome is not None and not outcome["products"]
                and planned.key.identifier_type == IDENTIFIER_EAN
                and planned.fallback is not None
                and planned.fallback not in seen):
            seen.add(planned.fallback)
            fallback_keys.append(planned.fallback)
    if fallback_keys:
        run_batches(fallback_keys, "fallback", used)
    return key_results


def _assemble_line_enrichments(review, plan, key_results, enrichment):
    lines_by_id = {ln.entity_id: ln for ln in review.lines}
    products: list[dict] = []
    product_index: dict[int, int] = {}

    def register(product: dict) -> int:
        pid = id(product)
        if pid not in product_index:
            product_index[pid] = len(products)
            products.append(product)
        return product_index[pid]

    for planned in plan.lookups:
        primary = key_results.get(planned.key, {"products": []})
        attempts = [{"identifier": planned.key.plu,
                     "identifier_type": planned.key.identifier_type,
                     "location_code": planned.key.location_code,
                     "price_date": planned.key.price_date,
                     "matched": len(primary["products"]) == 1,
                     "match_count": len(primary["products"])}]
        chosen = None
        chosen_type = planned.key.identifier_type
        if len(primary["products"]) == 1:
            chosen = primary["products"][0]
        elif len(primary["products"]) > 1:
            for line_id in planned.line_ids:
                enrichment["issues"].append({
                    "code": PRODUCT_MULTIPLE_MATCHES,
                    "severity": SEV_BLOCKING, "line_id": line_id,
                    "field": None, "source_value": planned.key.plu,
                    "api_value": None,
                    "message": f"{len(primary['products'])} products "
                               f"matched identifier '{planned.key.plu}'."})
        else:
            fb = (key_results.get(planned.fallback)
                  if planned.fallback is not None else None)
            if fb is not None:
                attempts.append({
                    "identifier": planned.fallback.plu,
                    "identifier_type": IDENTIFIER_CONSTRUCTED,
                    "location_code": planned.fallback.location_code,
                    "price_date": planned.fallback.price_date,
                    "matched": len(fb["products"]) == 1,
                    "match_count": len(fb["products"])})
                if len(fb["products"]) == 1:
                    chosen = fb["products"][0]
                    chosen_type = IDENTIFIER_CONSTRUCTED
                elif len(fb["products"]) > 1:
                    for line_id in planned.line_ids:
                        enrichment["issues"].append({
                            "code": PRODUCT_MULTIPLE_MATCHES,
                            "severity": SEV_BLOCKING, "line_id": line_id,
                            "field": None,
                            "source_value": planned.fallback.plu,
                            "api_value": None,
                            "message": "Multiple products matched the "
                                       "constructed fallback identifier."})
            if chosen is None and not any(
                    a["match_count"] > 1 for a in attempts):
                for line_id in planned.line_ids:
                    enrichment["issues"].append({
                        "code": PRODUCT_NOT_FOUND, "severity": SEV_BLOCKING,
                        "line_id": line_id, "field": None,
                        "source_value": planned.key.plu, "api_value": None,
                        "message": "No product found for "
                                   f"'{planned.key.plu}'"
                                   + (" or its constructed fallback"
                                      if planned.fallback is not None
                                      and len(attempts) > 1 else "")
                                   + " (the gateway omits unknown "
                                     "PLU-location combinations)."})

        product_ref = register(chosen) if chosen is not None else None
        for line_id in planned.line_ids:
            line = lines_by_id.get(line_id)
            comparison = (compare_line(line, chosen, chosen_type)
                          if chosen is not None and line is not None else [])
            enrichment["issues"].extend(comparison)
            enrichment["line_enrichments"].append({
                "line_id": line_id,
                "source_file": line.source_file if line else None,
                "source_page": line.source_page if line else None,
                "upload_sequence": line.upload_sequence if line else None,
                "delivery_note_number":
                    line.original.get("delivery_note_number") if line else None,
                "original_carton_number":
                    line.original.get("original_carton_number") if line else None,
                "source": {
                    "item_code": line.effective("item_code") if line else None,
                    "ean": line.effective("ean") if line else None,
                    "color_code": line.effective("color_code") if line else None,
                    "size_code": line.effective("size_code") if line else None,
                    "description": line.effective("description") if line else None,
                    "retail_price": line.effective("retail_price") if line else None,
                    "quantity": line.effective("quantity") if line else None,
                },
                "attempts": attempts,
                "matched_via": chosen_type if chosen is not None else None,
                "product_ref": product_ref,
                "status": ("matched" if chosen is not None else "unmatched"),
                "comparison_issue_count": len(comparison),
            })
    enrichment["products"] = products
    # untouched lines (no identifier) still appear, unmatched
    planned_line_ids = {lid for p in plan.lookups for lid in p.line_ids}
    for issue in enrichment["issues"]:
        if issue.get("code") == PRODUCT_LOOKUP_IDENTIFIER_MISSING:
            line = lines_by_id.get(issue.get("line_id"))
            if line is not None and line.entity_id not in planned_line_ids:
                enrichment["line_enrichments"].append({
                    "line_id": line.entity_id,
                    "source_file": line.source_file,
                    "source_page": line.source_page,
                    "upload_sequence": line.upload_sequence,
                    "delivery_note_number":
                        line.original.get("delivery_note_number"),
                    "original_carton_number":
                        line.original.get("original_carton_number"),
                    "source": {"item_code": line.effective("item_code"),
                               "ean": line.effective("ean"),
                               "color_code": line.effective("color_code"),
                               "size_code": line.effective("size_code"),
                               "description": line.effective("description"),
                               "retail_price": line.effective("retail_price"),
                               "quantity": line.effective("quantity")},
                    "attempts": [], "matched_via": None, "product_ref": None,
                    "status": "no_identifier", "comparison_issue_count": 0})


def _summarize(enrichment: dict) -> None:
    lines = enrichment["line_enrichments"]
    issues = enrichment["issues"]
    enrichment["summary"] = {
        "lines": len(lines),
        "matched_lines": sum(1 for l in lines if l["status"] == "matched"),
        "unmatched_lines": sum(1 for l in lines
                               if l["status"] == "unmatched"),
        "no_identifier_lines": sum(1 for l in lines
                                   if l["status"] == "no_identifier"),
        "matched_via_fallback": sum(
            1 for l in lines if l.get("matched_via") == IDENTIFIER_CONSTRUCTED
            and l["status"] == "matched"),
        "unique_products": len(enrichment["products"]),
        "batches": len(enrichment["batches"]),
        "blocking_issues": sum(1 for i in issues
                               if i.get("severity") == SEV_BLOCKING),
        "warning_issues": sum(1 for i in issues
                              if i.get("severity") == SEV_WARNING),
    }
