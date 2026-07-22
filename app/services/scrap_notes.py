"""Scrapbook Post-it notes."""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from sqlalchemy.orm import Session

from app.models.scrap_note import ScrapNote

NOTE_COLORS = ("yellow", "pink", "blue", "green", "orange")
MAX_PINNED = 6


def list_notes(db: Session) -> List[ScrapNote]:
    return (
        db.query(ScrapNote)
        .order_by(ScrapNote.pin_live.desc(), ScrapNote.sort_order.asc(), ScrapNote.id.desc())
        .all()
    )


def pinned_for_live_tiles(db: Session, limit: int = MAX_PINNED) -> List[ScrapNote]:
    return (
        db.query(ScrapNote)
        .filter(ScrapNote.pin_live.is_(True))
        .order_by(ScrapNote.sort_order.asc(), ScrapNote.id.desc())
        .limit(limit)
        .all()
    )


def create_note(
    db: Session,
    *,
    title: str = "",
    body: str = "",
    color: str = "yellow",
    pin_live: bool = False,
) -> ScrapNote:
    color = color if color in NOTE_COLORS else "yellow"
    if pin_live:
        _enforce_pin_cap(db, exclude_id=None)
    note = ScrapNote(
        title=(title or "").strip() or None,
        body=(body or "").strip() or None,
        color=color,
        pin_live=bool(pin_live),
        sort_order=0,
    )
    db.add(note)
    db.commit()
    db.refresh(note)
    return note


def update_note(
    db: Session,
    note_id: int,
    *,
    title: str = "",
    body: str = "",
    color: str = "yellow",
    pin_live: bool = False,
) -> Optional[ScrapNote]:
    note = db.query(ScrapNote).filter(ScrapNote.id == note_id).first()
    if not note:
        return None
    color = color if color in NOTE_COLORS else "yellow"
    if pin_live and not note.pin_live:
        _enforce_pin_cap(db, exclude_id=note_id)
    note.title = (title or "").strip() or None
    note.body = (body or "").strip() or None
    note.color = color
    note.pin_live = bool(pin_live)
    note.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(note)
    return note


def delete_note(db: Session, note_id: int) -> bool:
    note = db.query(ScrapNote).filter(ScrapNote.id == note_id).first()
    if not note:
        return False
    db.delete(note)
    db.commit()
    return True


def _enforce_pin_cap(db: Session, exclude_id: Optional[int]) -> None:
    """Keep at most MAX_PINNED-1 other pins when adding a new pin."""
    q = db.query(ScrapNote).filter(ScrapNote.pin_live.is_(True))
    if exclude_id:
        q = q.filter(ScrapNote.id != exclude_id)
    pinned = q.order_by(ScrapNote.updated_at.asc()).all()
    # room for one more
    overflow = len(pinned) - (MAX_PINNED - 1)
    for n in pinned[: max(0, overflow)]:
        n.pin_live = False
    if overflow > 0:
        db.commit()
