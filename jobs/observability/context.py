"""Validated correlation context, independent of authorization and task arguments."""

from collections.abc import Mapping
from contextvars import ContextVar
from types import MappingProxyType
from uuid import UUID, uuid4

context: ContextVar[Mapping[str, str]] = ContextVar(
    "observability_context", default=MappingProxyType({})
)


def identifier(value):
    if not isinstance(value, str) or len(value) != 36:
        return None
    try:
        parsed = UUID(value)
        return str(parsed) if str(parsed) == value.lower() else None
    except ValueError:
        return None


def new_context(headers=None):
    headers = headers if isinstance(headers, Mapping) else {}
    return {
        "request_id": identifier(headers.get("request_id")) or str(uuid4()),
        "workflow_id": identifier(headers.get("workflow_id")) or str(uuid4()),
    }


def outbound_headers():
    return {
        f"X-Jobstream-{key.replace('_', '-')}": value
        for key, value in context.get().items()
        if key in {"request_id", "workflow_id"} and identifier(value)
    }
