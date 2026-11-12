"""Synthetic demo data. Run: python -m app.fixtures.seed  (idempotent; lists the demo accounts, never a password).

Two organisations prove tenant isolation. The password comes from
DEMO_PASSWORD (environment or .env); it is never generated or printed.
Never use these fixtures with real customer data.
"""

from __future__ import annotations

import os
import sys
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.db.session import get_sessionmaker, set_auth_scope, set_tenant
from app.models.customer import Customer
from app.models.org import Membership, Organization, User
from app.services import catalog_import

ROLES = ("owner", "admin", "sales", "approver", "readonly")
DEMO_ORGS: list[dict[str, Any]] = [
    {
        "slug": "northwind",
        "name": "Northwind Trading LLC",
        "legal_name": "Northwind Trading L.L.C.",
        "address": "Warehouse 12, Al Quoz Industrial Area 3\nDubai, United Arab Emirates",
        "default_currency": "AED",
        "default_tax_rate": Decimal("5"),
        "min_margin_pct": Decimal("12"),
        "brand_color": "#1f4e79",
        "pdf_footer": "Prices in AED excluding delivery. Payment terms 30 days net. Synthetic demo organisation.",
        "customers": [
            {
                "name": "Gulf Fabrication Co.",
                "reference": "GFC-001",
                "email": "procurement@gulffab.example",
                "default_currency": "AED",
            },
            {
                "name": "Desert Rose Facilities",
                "reference": "DRF-014",
                "email": "buying@desertrose.example",
                "default_currency": "AED",
            },
        ],
    },
    {
        "slug": "contoso",
        "name": "Contoso Supplies GmbH",
        "legal_name": "Contoso Supplies GmbH",
        "address": "Industriestraße 5\n20457 Hamburg, Germany",
        "default_currency": "EUR",
        "default_tax_rate": Decimal("19"),
        "min_margin_pct": Decimal("15"),
        "brand_color": "#7a1f2b",
        "pdf_footer": "Preise in EUR zzgl. Versand. Synthetic demo organisation.",
        "customers": [
            {
                "name": "Nordsee Werft AG",
                "reference": "NW-2201",
                "email": "einkauf@nordsee.example",
                "default_currency": "EUR",
            },
        ],
    },
]


_REPO_ROOT_ENV = Path(__file__).resolve().parents[4] / ".env"


def _read_demo_password(path: Path) -> str | None:
    """Only ``DEMO_PASSWORD`` is read from a `.env` file, by a deliberately tiny reader: the file is not validated as
    `Settings` (an unrelated invalid value must not crash the seed or be echoed, SEC7-4). A missing, unreadable,
    undecodable or malformed file counts as absent. Understands ``KEY=value``, ``export KEY=value`` and a value in
    matching single or double quotes (like python-dotenv); a later assignment wins; comments and other keys are
    ignored."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError, ValueError):
        return None
    value: str | None = None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, rest = line.partition("=")
        if not sep or key.strip() != "DEMO_PASSWORD":
            continue
        rest = rest.strip()
        if len(rest) >= 2 and rest[0] == rest[-1] and rest[0] in "\"'":
            quote, inner = rest[0], rest[1:-1]
            value = inner.replace('\\"', '"').replace("\\\\", "\\") if quote == '"' else inner
        else:
            value = rest.split(" #", 1)[0].rstrip()
    return value or None


def _password() -> str | None:
    """The demo password is chosen by the person running the seed (DEMO_PASSWORD in the environment or `.env`).
    The seed never generates one and never prints one: a credential, even a synthetic one, does not belong in a
    console or container log. Precedence: a non-empty environment variable, then `.env` in the working directory,
    then `.env` at the repository root (`make demo` runs from apps/api while the documented quick start creates the
    file at the root). An empty value never masks a later source."""
    chosen = os.environ.get("DEMO_PASSWORD")
    if not chosen:
        for path in (Path.cwd() / ".env", _REPO_ROOT_ENV):
            chosen = _read_demo_password(path)
            if chosen:
                break
    return chosen or None


def seed(db: Session, password: str, verbose: bool = True) -> dict[str, dict[str, str]]:
    created: dict[str, dict[str, str]] = {}
    csv_bytes = (Path(__file__).parent / "data" / "demo_catalog.csv").read_bytes()
    for spec in DEMO_ORGS:
        org = db.scalar(select(Organization).where(Organization.name == spec["name"]))
        if org is None:
            org = Organization(
                id=uuid.uuid4(),
                name=spec["name"],
                legal_name=spec["legal_name"],
                address=spec["address"],
                default_currency=spec["default_currency"],
                default_tax_rate=spec["default_tax_rate"],
                min_margin_pct=spec["min_margin_pct"],
                brand_color=spec["brand_color"],
                pdf_footer=spec["pdf_footer"],
            )
            set_tenant(db, org.id, None)  # RLS WITH CHECK needs the tenant context before inserting the org row
            db.add(org)
            db.flush()
        set_tenant(db, org.id, None)
        accounts: dict[str, str] = {}
        owner_user: User | None = None
        for role in ROLES:
            email = f"{role}@{spec['slug']}.example"
            set_auth_scope(db, login_email=email)  # users are RLS-scoped; make this address visible for the lookup
            user = db.scalar(select(User).where(User.email == email))
            if user is None:
                user = User(
                    email=email,
                    password_hash=hash_password(password),
                    full_name=f"{role.capitalize()} {spec['slug'].capitalize()}",
                )
                db.add(user)
                db.flush()
            if (
                db.scalar(select(Membership).where(Membership.organization_id == org.id, Membership.user_id == user.id))
                is None
            ):
                db.add(Membership(organization_id=org.id, user_id=user.id, role=role))
            accounts[role] = email
            if role == "owner":
                owner_user = user
        db.flush()
        set_tenant(db, org.id, owner_user.id if owner_user else None)
        for c in spec["customers"]:
            if (
                db.scalar(select(Customer).where(Customer.organization_id == org.id, Customer.name == c["name"]))
                is None
            ):
                db.add(Customer(organization_id=org.id, **c))
        db.flush()
        from app.models.catalog import Catalog

        if db.scalar(select(Catalog).where(Catalog.organization_id == org.id)) is None and owner_user is not None:
            # Contoso's catalog is the same SKU set priced in EUR (synthetic): rewrite the currency column
            content = csv_bytes if spec["default_currency"] == "AED" else csv_bytes.replace(b",AED,", b",EUR,")
            version = catalog_import.import_catalog(
                db,
                organization_id=org.id,
                actor_user_id=owner_user.id,
                ip=None,
                filename="demo_catalog.csv",
                content=content,
                mapping={
                    "sku": "SKU",
                    "description": "Description",
                    "unit": "Unit",
                    "currency": "Currency",
                    "list_price": "List Price",
                    "cost_price": "Cost Price",
                    "stock_qty": "Stock",
                    "valid_from": "Valid From",
                    "valid_to": "Valid To",
                },
                catalog_name="Main catalog",
                default_currency=spec["default_currency"],
            )
            catalog_import.activate_version(
                db, organization_id=org.id, actor_user_id=owner_user.id, ip=None, version_id=version.id
            )
        db.commit()
        created[spec["name"]] = accounts
    if verbose:
        print("Synthetic demo data ready. LOCAL-ONLY credentials (never reuse elsewhere):")
        for org_name, accounts in created.items():
            print(f"  {org_name}:")
            for role, email in accounts.items():
                print(f"    {role:9s} {email}")
        print("  password for all demo accounts: the value of DEMO_PASSWORD (not shown)")
        print("  accounts that already existed keep the password they were created with")
    return created


def main() -> int:
    password = _password()
    if password is None or len(password) < 12:
        print(
            "Set DEMO_PASSWORD (at least 12 characters) in the environment or in .env before seeding. "
            "The seed does not generate or print a password.",
            file=sys.stderr,
        )
        return 2
    with get_sessionmaker()() as db:
        seed(db, password)
    return 0


if __name__ == "__main__":
    sys.exit(main())
