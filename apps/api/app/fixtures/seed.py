"""Synthetic demo data. Run: python -m app.fixtures.seed  (idempotent; prints local-only credentials).

Two organisations prove tenant isolation. Passwords come from DEMO_PASSWORD (env) or a generated value printed once.
Never use these fixtures with real customer data.
"""

from __future__ import annotations

import os
import secrets
import sys
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.db.session import get_sessionmaker, set_tenant
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


def _password() -> str:
    pw = os.environ.get("DEMO_PASSWORD")
    if pw:
        return pw
    return "Demo-" + secrets.token_urlsafe(12)


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
        print(f"  password for all demo accounts: {password}")
    return created


def main() -> int:
    password = _password()
    if len(password) < 12:
        print("DEMO_PASSWORD must be at least 12 characters", file=sys.stderr)
        return 2
    with get_sessionmaker()() as db:
        seed(db, password)
    return 0


if __name__ == "__main__":
    sys.exit(main())
