"""Deterministic, placeholder-whitelisted template rendering (S2-F04). No code execution, no HTML."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from app.core.errors import ValidationFailed
from app.services.money import minor_units

ALLOWED_PLACEHOLDERS = {
    "contact_name",
    "customer_name",
    "quote_number",
    "quote_version",
    "grand_total",
    "currency",
    "valid_until",
    "org_name",
    "sender_name",
    "attempt_no",
    "days_since_sent",
    "opt_out_text",
}
_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
OPT_OUT_TEXT = (
    "If you prefer not to receive follow-up messages about quotations from {org_name}, reply with the word "
    "UNSUBSCRIBE and we will stop."
)

DEFAULT_TEMPLATES: list[dict[str, str]] = [
    {
        "key": "follow_up_1",
        "name": "First follow-up",
        "subject": "Quotation {quote_number} — any questions?",
        "body": (
            "Dear {contact_name},\n\nWe sent you quotation {quote_number} (total {grand_total} {currency}, valid until "
            "{valid_until}) {days_since_sent} days ago. Is there anything we can clarify or adjust?\n\n"
            "Kind regards,\n{sender_name}\n{org_name}\n\n{opt_out_text}"
        ),
    },
    {
        "key": "follow_up_2",
        "name": "Second follow-up",
        "subject": "Following up on quotation {quote_number}",
        "body": (
            "Dear {contact_name},\n\nA short reminder about quotation {quote_number} for {customer_name}. "
            "The prices are valid until {valid_until}. We are happy to discuss quantities or delivery.\n\n"
            "Kind regards,\n{sender_name}\n{org_name}\n\n{opt_out_text}"
        ),
    },
    {
        "key": "expiry_reminder",
        "name": "Validity about to expire",
        "subject": "Quotation {quote_number} expires on {valid_until}",
        "body": (
            "Dear {contact_name},\n\nQuotation {quote_number} ({grand_total} {currency}) expires on {valid_until}. "
            "If you would like to proceed or need an extension, please let us know.\n\n"
            "Kind regards,\n{sender_name}\n{org_name}\n\n{opt_out_text}"
        ),
    },
]


@dataclass(frozen=True)
class TemplateContext:
    contact_name: str
    customer_name: str
    quote_number: str
    quote_version: int
    grand_total: Decimal
    currency: str
    valid_until: str
    org_name: str
    sender_name: str
    attempt_no: int
    days_since_sent: int

    def as_mapping(self) -> dict[str, str]:
        places = minor_units(self.currency)
        total = f"{self.grand_total:,.{places}f}"
        return {
            "contact_name": self.contact_name,
            "customer_name": self.customer_name,
            "quote_number": self.quote_number,
            "quote_version": str(self.quote_version),
            "grand_total": total,
            "currency": self.currency,
            "valid_until": self.valid_until,
            "org_name": self.org_name,
            "sender_name": self.sender_name,
            "attempt_no": str(self.attempt_no),
            "days_since_sent": str(self.days_since_sent),
            "opt_out_text": OPT_OUT_TEXT.format(org_name=self.org_name),
        }


def validate_template(text: str) -> None:
    unknown = sorted({m for m in _PLACEHOLDER.findall(text) if m not in ALLOWED_PLACEHOLDERS})
    if unknown:
        raise ValidationFailed(f"Unknown placeholders: {', '.join(unknown)}", field="body")
    if re.search(r"<\s*script|javascript:", text, re.IGNORECASE):
        raise ValidationFailed("Templates are plain text; scripts are not allowed", field="body")


def render(text: str, ctx: TemplateContext) -> str:
    validate_template(text)
    mapping = ctx.as_mapping()
    return _PLACEHOLDER.sub(lambda m: mapping.get(m.group(1), m.group(0)), text)


def ensure_opt_out(body: str, ctx: TemplateContext) -> str:
    """Every outbound follow-up carries the opt-out instruction, even if a template dropped it."""
    return ensure_opt_out_for(body, ctx.org_name)


def ensure_opt_out_for(body: str, org_name: str) -> str:
    """Append the mandatory opt-out instruction unless it is already present (idempotent)."""
    opt = OPT_OUT_TEXT.format(org_name=org_name)
    return body if opt in body else body.rstrip() + "\n\n" + opt
