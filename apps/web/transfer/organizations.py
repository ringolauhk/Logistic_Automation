"""Organization catalog for the Transfer workflow (Build 11).

The API login account in `.env` is a shared/default account and is NOT
tied to one organization, so the user selects the organization explicitly
in the Transfer UI; its Organization ID is sent as `orgId` on every
item-master lookup request. The lookup organization is NEVER
derived from the authenticated API user, the access token, or `.env`.

The approved business mapping below is the single runtime source of truth
(deterministic, typed - the production UI never reads an Excel file at
runtime). `organization.xlsx` in the repository root is a human reference
copy; a test cross-checks it against this catalog when present.

Organization IDs are strings, matching the `orgId` string type in the
tracked OpenAPI snapshot (docs/api/imaginex-api-swagger-v1.json).
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Organization:
    name: str
    org_id: str


# Approved functional mapping (Build 11) - names sorted for display.
ORGANIZATIONS: tuple[Organization, ...] = (
    Organization("Brooks Brothers Hong Kong", "100017"),
    Organization("Brooks Brothers Macao", "100018"),
    Organization("Brooks Brothers Malaysia", "100023"),
    Organization("Brooks Brothers Singapore", "100022"),
    Organization("Brooks Brothers Taiwan", "100021"),
    Organization("IMAGINEX Hong Kong", "100009"),
    Organization("IMAGINEX Macao", "100010"),
    Organization("IMAGINEX Singapore", "100014"),
    Organization("IMAGINEX Taiwan", "100012"),
    Organization("Joyce Beauty", "100007"),
    Organization("Sacai Hong Kong", "100024"),
)

_BY_NAME = {org.name: org for org in ORGANIZATIONS}
_BY_ID = {org.org_id: org for org in ORGANIZATIONS}


def organization_names() -> list[str]:
    return [org.name for org in ORGANIZATIONS]


def by_name(name: str) -> Organization | None:
    return _BY_NAME.get((name or "").strip())


def by_id(org_id: str) -> Organization | None:
    return _BY_ID.get(str(org_id or "").strip())


def is_valid_org_id(org_id) -> bool:
    return by_id(org_id) is not None
