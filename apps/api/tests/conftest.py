"""Shared fixtures: real PostgreSQL 16 test database, migrations, per-test truncation, authenticated clients."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault(
    "DATABASE_URL",
    os.environ.get(
        "DATABASE_URL_TEST", "postgresql+psycopg://quotepilot_app:quotepilot_app@localhost:5432/quotepilot_test"
    ),
)
os.environ.setdefault(
    "DATABASE_ADMIN_URL",
    os.environ.get(
        "DATABASE_ADMIN_URL_TEST",
        "postgresql+psycopg://quotepilot_owner:quotepilot_owner@localhost:5432/quotepilot_test",
    ),
)
os.environ.setdefault("SECRET_KEY", "test-secret-key-test-secret-key-test-secret-key")
_STORAGE_TMP = tempfile.mkdtemp(prefix="qp-storage-")
os.environ["STORAGE_DIR"] = _STORAGE_TMP
os.environ.setdefault("LOG_LEVEL", "WARNING")
# the suite creates far more RFQs / imports per user than the production limits allow (SEC7-7); the limiter has its
# own tests with low limits (tests/integration/test_round11_fixes.py)
os.environ.setdefault("RFQ_PARSE_RATE_LIMIT_ATTEMPTS", "1000000")
os.environ.setdefault("CATALOG_IMPORT_RATE_LIMIT_ATTEMPTS", "1000000")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.db.session import get_sessionmaker, reset_engine  # noqa: E402
from app.services.auth import account_limiter, login_limiter  # noqa: E402
from app.services.ratelimit import action_limiter  # noqa: E402
from app.services.storage import reset_storage  # noqa: E402

API = "/api/v1"
FIXTURES = Path(__file__).resolve().parents[3] / "tests" / "fixtures"
API_DIR = Path(__file__).resolve().parents[1]
PASSWORD = "Correct-Horse-Battery-9!"


def _admin_engine():  # type: ignore[no-untyped-def]
    return create_engine(os.environ["DATABASE_ADMIN_URL"], future=True, isolation_level="AUTOCOMMIT")


@pytest.fixture(scope="session", autouse=True)
def migrated_database() -> Iterator[None]:
    """Run alembic migrations as the owner role once per session (this doubles as the migration test)."""
    env = {**os.environ}
    r = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=API_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, r.stderr[-2000:]
    yield


@pytest.fixture(autouse=True)
def clean_tables(migrated_database: None) -> Iterator[None]:
    eng = _admin_engine()
    with eng.connect() as conn:
        tables = [
            t
            for (t,) in conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename <> 'alembic_version'")
            )
        ]
        if tables:
            conn.execute(text("TRUNCATE " + ", ".join(tables) + " RESTART IDENTITY CASCADE"))
    eng.dispose()
    login_limiter.reset()
    account_limiter.reset()
    action_limiter.reset()
    reset_storage()
    get_settings.cache_clear()
    yield
    reset_engine()


@pytest.fixture
def app():  # type: ignore[no-untyped-def]
    from app.main import create_app

    return create_app()


@pytest.fixture
def client(app) -> Iterator[TestClient]:  # type: ignore[no-untyped-def]
    with TestClient(app, base_url="http://testserver") as c:
        yield c


@dataclass
class Tenant:
    org_id: uuid.UUID
    name: str
    users: dict[str, str]  # role → email
    customer_id: uuid.UUID
    catalog_version_id: uuid.UUID | None


def make_tenant(
    name: str, currency: str = "AED", *, with_catalog: bool = True, min_margin: str = "12", tax: str = "5"
) -> Tenant:
    """Create an organisation with one user per role, one customer and (optionally) the demo catalog."""
    from decimal import Decimal

    from app.core.security import hash_password
    from app.db.session import set_tenant
    from app.models.customer import Customer
    from app.models.org import Membership, Organization, User
    from app.services import catalog_import

    slug = name.lower().replace(" ", "")
    with get_sessionmaker()() as db:
        org = Organization(
            id=uuid.uuid4(),
            name=name,
            legal_name=f"{name} LLC",
            address="1 Demo Street",
            default_currency=currency,
            default_tax_rate=Decimal(tax),
            min_margin_pct=Decimal(min_margin),
            pdf_footer="Synthetic",
        )
        set_tenant(db, org.id, None)  # RLS WITH CHECK requires the tenant context before the org row is inserted
        db.add(org)
        db.flush()
        users: dict[str, str] = {}
        owner_id = None
        for role in ("owner", "admin", "sales", "approver", "readonly"):
            email = f"{role}@{slug}.example"
            user = User(email=email, password_hash=hash_password(PASSWORD), full_name=f"{role} {name}")
            db.add(user)
            db.flush()
            db.add(Membership(organization_id=org.id, user_id=user.id, role=role))
            users[role] = email
            if role == "owner":
                owner_id = user.id
        db.flush()
        set_tenant(db, org.id, owner_id)
        customer = Customer(
            organization_id=org.id, name=f"{name} Customer", reference="CUST-1", default_currency=currency
        )
        db.add(customer)
        db.flush()
        version_id = None
        if with_catalog:
            csv_bytes = (API_DIR / "app" / "fixtures" / "data" / "demo_catalog.csv").read_bytes()
            if currency != "AED":
                csv_bytes = csv_bytes.replace(b",AED,", f",{currency},".encode())
            version = catalog_import.import_catalog(
                db,
                organization_id=org.id,
                actor_user_id=owner_id,  # type: ignore[arg-type]
                ip=None,
                filename="demo_catalog.csv",
                content=csv_bytes,
                mapping={
                    "sku": "SKU",
                    "description": "Description",
                    "unit": "Unit",
                    "currency": "Currency",
                    "list_price": "List Price",
                    "cost_price": "Cost Price",
                    "stock_qty": "Stock",
                },
                catalog_name="Main",
                default_currency=currency,
            )
            catalog_import.activate_version(
                db, organization_id=org.id, actor_user_id=owner_id, ip=None, version_id=version.id
            )  # type: ignore[arg-type]
            version_id = version.id
        db.commit()
        return Tenant(org_id=org.id, name=name, users=users, customer_id=customer.id, catalog_version_id=version_id)


@pytest.fixture
def tenant_a() -> Tenant:
    return make_tenant("Alpha Trading")


@pytest.fixture
def tenant_b() -> Tenant:
    return make_tenant("Beta Supplies", currency="EUR")


class AuthedClient:
    """Wraps a TestClient with a logged-in session and automatic CSRF header."""

    def __init__(self, client: TestClient, email: str, password: str = PASSWORD) -> None:
        self.client = client
        self.email = email
        pre = client.get(f"{API}/auth/csrf")
        assert pre.status_code == 200
        pre_token = pre.json()["csrf_token"]
        r = client.post(
            f"{API}/auth/login", json={"email": email, "password": password}, headers={"X-CSRF-Token": pre_token}
        )
        assert r.status_code == 200, r.text
        self.info = r.json()
        self.csrf = self.info["csrf_token"]
        self.role = self.info["role"]
        self.user_id = self.info["user"]["id"]

    def _h(self, headers: dict[str, str] | None) -> dict[str, str]:
        return {"X-CSRF-Token": self.csrf, **(headers or {})}

    def get(self, path: str, **kw):  # type: ignore[no-untyped-def]
        return self.client.get(f"{API}{path}", **kw)

    def post(self, path: str, headers: dict[str, str] | None = None, **kw):  # type: ignore[no-untyped-def]
        return self.client.post(f"{API}{path}", headers=self._h(headers), **kw)

    def patch(self, path: str, headers: dict[str, str] | None = None, **kw):  # type: ignore[no-untyped-def]
        return self.client.patch(f"{API}{path}", headers=self._h(headers), **kw)

    def delete(self, path: str, headers: dict[str, str] | None = None, **kw):  # type: ignore[no-untyped-def]
        return self.client.delete(f"{API}{path}", headers=self._h(headers), **kw)


def login_as(app, email: str) -> AuthedClient:  # type: ignore[no-untyped-def]
    """Each authed client gets its own cookie jar (separate TestClient)."""
    client = TestClient(app, base_url="http://testserver")
    return AuthedClient(client, email)


def upload(
    ac: AuthedClient,
    path: str,
    filename: str,
    content: bytes,
    fields: dict[str, str] | None = None,
    content_type: str = "application/octet-stream",
):  # type: ignore[no-untyped-def]
    return ac.post(path, files={"file": (filename, content, content_type)}, data=fields or {})
