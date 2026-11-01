"""Rule-based follow-up recommendations (S2-F03). Every recommendation lists the facts it is based on."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.db.base import utcnow
from app.models.followup import CommunicationEvent, Contact, FollowUpPolicy, FollowUpSchedule
from app.models.quote import Quote
from app.services.lifecycle import effective_lifecycle_status


@dataclass
class Recommendation:
    action: str  # send_follow_up | offer_extension | record_outcome | none | wait
    template_key: str | None
    priority: str  # high | normal | low
    reasons: list[str] = field(default_factory=list)
    blocked_by: list[str] = field(default_factory=list)
    suggested_contact_id: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "template_key": self.template_key,
            "priority": self.priority,
            "reasons": self.reasons,
            "blocked_by": self.blocked_by,
            "suggested_contact_id": self.suggested_contact_id,
        }


def _last_outbound(events: list[CommunicationEvent]) -> datetime | None:
    outs = [e.occurred_at for e in events if e.direction == "out" and e.kind == "sent"]
    return max(outs) if outs else None


def recommend(
    quote: Quote,
    *,
    policy: FollowUpPolicy | None,
    contacts: list[Contact],
    events: list[CommunicationEvent],
    schedules: list[FollowUpSchedule],
    now: datetime | None = None,
) -> Recommendation:
    now = now or utcnow()
    status = effective_lifecycle_status(quote)
    reasons: list[str] = [f"quote status is '{status}'"]
    if status in ("draft", "pending_approval"):
        return Recommendation("none", None, "low", reasons + ["quote is not approved yet"])
    if status in ("won", "lost", "withdrawn"):
        return Recommendation("none", None, "low", reasons + ["lifecycle is closed"])
    if status == "expired":
        return Recommendation(
            "record_outcome",
            None,
            "high",
            reasons + ["validity date has passed; record won/lost or create a new version"],
        )
    if status == "approved":
        return Recommendation(
            "none", None, "normal", reasons + ["quote has not been marked as sent; send it or record it as sent first"]
        )

    inbound = [e for e in events if e.direction == "in"]
    if inbound:
        last_in = max(e.occurred_at for e in inbound)
        reasons.append(f"customer reply recorded on {last_in.date().isoformat()}")
        if status != "won":
            return Recommendation(
                "wait",
                None,
                "normal",
                reasons + ["a reply exists; respond personally rather than sending an automated follow-up"],
            )

    eligible = [c for c in contacts if c.email and c.opted_out_at is None and c.preferred_channel != "none"]
    opted_out = [c for c in contacts if c.opted_out_at is not None]
    blocked: list[str] = []
    if opted_out:
        reasons.append(f"{len(opted_out)} contact(s) opted out and are excluded")
    if not eligible:
        blocked.append("no contact with an e-mail address who has not opted out")

    sent_count = len([e for e in events if e.direction == "out" and e.kind == "sent"])
    last_out = _last_outbound(events)
    days_since = (now - last_out).days if last_out else None
    if last_out:
        reasons.append(f"last outbound message {days_since} day(s) ago; {sent_count} follow-up(s) sent so far")
    else:
        reasons.append("no outbound follow-up recorded since the quote was sent")
    if policy is None:
        blocked.append("no follow-up policy configured")
        return Recommendation("send_follow_up", "follow_up_1", "normal", reasons, blocked)
    if sent_count >= policy.max_attempts:
        return Recommendation(
            "record_outcome", None, "normal", reasons + [f"maximum of {policy.max_attempts} follow-ups reached"]
        )
    offsets = list(policy.offsets_days) or [3]
    idx = min(sent_count, len(offsets) - 1)
    due_days = offsets[idx]
    pending = [s for s in schedules if s.status == "pending"]
    if pending:
        reasons.append(f"a follow-up is already scheduled for {min(s.due_at for s in pending).date().isoformat()}")
        return Recommendation("wait", None, "low", reasons)
    days_left = (quote.valid_until - now.date()).days if quote.valid_until else None
    if days_left is not None and 0 <= days_left <= 3:
        reasons.append(f"validity ends in {days_left} day(s)")
        primary = next((c for c in eligible if c.is_primary), eligible[0] if eligible else None)
        return Recommendation(
            "send_follow_up", "expiry_reminder", "high", reasons, blocked, str(primary.id) if primary else None
        )
    if days_since is None or days_since >= due_days:
        reasons.append(f"policy offset for follow-up #{sent_count + 1} is {due_days} day(s)")
        primary = next((c for c in eligible if c.is_primary), eligible[0] if eligible else None)
        key = "follow_up_1" if sent_count == 0 else "follow_up_2"
        return Recommendation("send_follow_up", key, "normal", reasons, blocked, str(primary.id) if primary else None)
    return Recommendation("wait", None, "low", reasons + [f"next follow-up is due after {due_days} day(s) per policy"])
