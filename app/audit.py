"""Audit log helpers: who changed what, old -> new.

Typical use in a router (inside the request's DB session, before commit)::

    changes = audit.apply_changes(reading, {"value": new_value, "note": note})
    if changes:
        audit.record_update(db, user, reading, changes)
    db.commit()
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from sqlalchemy import inspect
from sqlalchemy.orm import Session

from app.models import AuditLog, User


def jsonable(v: Any) -> Any:
    """Convert values (Decimal, dates, enums, ...) to JSON-safe primitives."""
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Enum):
        return v.value
    if isinstance(v, Mapping):
        return {str(k): jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [jsonable(x) for x in v]
    return str(v)


def entity_type_of(obj: Any) -> str:
    return getattr(obj, "__tablename__", type(obj).__name__)


def record(
    db: Session,
    user: User | None,
    action: str,
    entity_type: str,
    entity_id: int | None,
    changes: Mapping[str, Any] | None = None,
    note: str = "",
    user_email: str | None = None,
) -> AuditLog:
    entry = AuditLog(
        user_id=user.id if user else None,
        user_email=user_email if user_email is not None else (user.email if user else ""),
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        changes=jsonable(changes) if changes is not None else None,
        note=note,
    )
    db.add(entry)
    return entry


def snapshot(obj: Any) -> dict[str, Any]:
    """Column values of an ORM object."""
    mapper = inspect(obj).mapper
    return {c.key: getattr(obj, c.key) for c in mapper.column_attrs}


def record_create(db: Session, user: User | None, obj: Any, note: str = "") -> AuditLog:
    db.flush()  # ensure obj.id
    return record(db, user, "create", entity_type_of(obj), obj.id, snapshot(obj), note)


def record_delete(db: Session, user: User | None, obj: Any, note: str = "") -> AuditLog:
    return record(db, user, "delete", entity_type_of(obj), obj.id, snapshot(obj), note)


def record_update(
    db: Session,
    user: User | None,
    obj: Any,
    changes: Mapping[str, tuple[Any, Any]],
    note: str = "",
    user_email: str | None = None,
) -> AuditLog:
    """``changes`` = {field: (old, new)}; stored as {field: [old, new]}."""
    return record(
        db,
        user,
        "update",
        entity_type_of(obj),
        obj.id,
        {k: [old, new] for k, (old, new) in changes.items()},
        note,
        user_email,
    )


def apply_changes(obj: Any, values: Mapping[str, Any]) -> dict[str, tuple[Any, Any]]:
    """Set attributes on ``obj`` and return {field: (old, new)} for those that changed."""
    changes: dict[str, tuple[Any, Any]] = {}
    for k, new in values.items():
        old = getattr(obj, k)
        if old != new:
            changes[k] = (old, new)
            setattr(obj, k, new)
    return changes
