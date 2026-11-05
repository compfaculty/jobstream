"""Versioned runtime contract. No Infinity classes cross this boundary."""

from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from django.conf import settings

VERSION = 1
TERMINAL = {"completed", "failed", "cancelled"}


class GatewayError(Exception):
    def __init__(self, code: str, retryable: bool = False, delay: int = 60):
        self.code, self.retryable, self.delay = code, retryable, delay
        super().__init__(code)


@dataclass(frozen=True)
class ExecutionRequest:
    execution_id: str
    attempt_id: str
    correlation_id: str
    model: str
    capability: str
    input_hash: str
    max_input_characters: int
    max_output_tokens: int
    deadline: str
    schema_version: int = VERSION


@dataclass(frozen=True)
class ExecutionResponse:
    execution_id: str
    attempt_id: str
    correlation_id: str
    status: Literal["running", "completed", "failed", "cancelled"]
    result: dict
    usage: dict
    error_code: str
    retryable: bool
    retry_after: int

    @classmethod
    def parse(cls, data: object, execution_id: str, attempt_id: str) -> ExecutionResponse:
        expected = {
            "schema_version",
            "execution_id",
            "attempt_id",
            "correlation_id",
            "status",
            "result",
            "usage",
            "error_code",
            "retryable",
            "retry_after",
        }
        if not isinstance(data, dict) or set(data) != expected:
            raise GatewayError("agent_invalid_contract")
        if (
            type(data["schema_version"]) is not int
            or data["schema_version"] != VERSION
            or data["execution_id"] != execution_id
            or data["attempt_id"] != attempt_id
            or data["correlation_id"] != execution_id
            or data["status"] not in {"running", *TERMINAL}
            or not isinstance(data["result"], dict)
            or not isinstance(data["usage"], dict)
            or not isinstance(data["error_code"], str)
            or len(data["error_code"]) > 80
            or type(data["retryable"]) is not bool
            or type(data["retry_after"]) is not int
            or not 0 <= data["retry_after"] <= 3600
        ):
            raise GatewayError("agent_invalid_contract")
        for key, value in data["usage"].items():
            if key not in {"input_tokens", "output_tokens"} or type(value) is not int or value < 0:
                raise GatewayError("agent_invalid_usage")
        return cls(**{key: value for key, value in data.items() if key != "schema_version"})


class AgentGateway:
    def call(
        self, method: str, execution_id: str, attempt_id: str, payload=None
    ) -> ExecutionResponse:
        UUID(execution_id)
        UUID(attempt_id)
        url = urlsplit(settings.INFINITY_URL)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path not in {"", "/"}
            or not settings.INFINITY_SERVICE_TOKEN
        ):
            raise GatewayError("agent_not_configured")
        path = f"/v1/executions/{execution_id}/{attempt_id}"
        try:
            with httpx.Client(timeout=10, follow_redirects=False, trust_env=False) as client:
                with client.stream(
                    method,
                    settings.INFINITY_URL.rstrip("/") + path,
                    headers={"Authorization": f"Bearer {settings.INFINITY_SERVICE_TOKEN}"},
                    json=payload,
                ) as response:
                    if response.status_code == 404:
                        raise GatewayError("agent_not_found", True)
                    if response.status_code in {429, 503}:
                        raise GatewayError("agent_busy", True)
                    if response.status_code >= 500:
                        raise GatewayError("agent_service_error", True)
                    if not 200 <= response.status_code < 300:
                        raise GatewayError("agent_request_rejected")
                    chunks, size = [], 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > 256000:
                            raise GatewayError("agent_response_too_large")
                        chunks.append(chunk)
            import json

            return ExecutionResponse.parse(json.loads(b"".join(chunks)), execution_id, attempt_id)
        except httpx.HTTPError as exc:
            raise GatewayError("agent_unavailable", True) from exc
        except (ValueError, TypeError) as exc:
            raise GatewayError("agent_invalid_contract") from exc

    def start(self, request: ExecutionRequest) -> ExecutionResponse:
        from dataclasses import asdict

        return self.call("PUT", request.execution_id, request.attempt_id, asdict(request))

    def status(self, execution_id: str, attempt_id: str) -> ExecutionResponse:
        return self.call("GET", execution_id, attempt_id)

    def cancel(self, execution_id: str, attempt_id: str) -> ExecutionResponse:
        return self.call("DELETE", execution_id, attempt_id)

    def provider_calls(self, execution_id: str) -> list[dict]:
        """Separate metadata contract; do not loosen execution-response validation."""
        import json
        from datetime import datetime

        UUID(execution_id)
        origin = urlsplit(settings.INFINITY_URL)
        if (
            origin.scheme not in {"http", "https"}
            or not origin.hostname
            or origin.username
            or origin.password
            or origin.query
            or origin.fragment
            or origin.path not in {"", "/"}
            or not settings.INFINITY_SERVICE_TOKEN
        ):
            raise GatewayError("agent_not_configured")
        try:
            with httpx.Client(timeout=2, follow_redirects=False, trust_env=False) as client:
                with client.stream(
                    "GET",
                    settings.INFINITY_URL.rstrip("/")
                    + f"/v1/operations/executions/{execution_id}/calls",
                    headers={"Authorization": f"Bearer {settings.INFINITY_SERVICE_TOKEN}"},
                ) as response:
                    if response.status_code != 200:
                        raise GatewayError("agent_metadata_unavailable")
                    chunks, size = [], 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > 256000:
                            raise GatewayError("agent_response_too_large")
                        chunks.append(chunk)
            data = json.loads(b"".join(chunks))
            if (
                not isinstance(data, dict)
                or type(data.get("schema_version")) is not int
                or data.get("schema_version") != 1
                or data.get("execution_id") != execution_id
                or not isinstance(data.get("calls"), list)
                or len(data["calls"]) > 100
            ):
                raise ValueError
            fields = {
                "call_id",
                "execution_id",
                "attempt_id",
                "model",
                "started_at",
                "finished_at",
                "latency_ms",
                "outcome",
                "error_code",
                "input_tokens",
                "output_tokens",
                "provider_request_id",
            }
            calls = []
            for call in data["calls"]:
                if not isinstance(call, dict) or not fields.issubset(call):
                    raise ValueError
                if call["execution_id"] != execution_id:
                    raise ValueError
                for key in ("call_id", "attempt_id"):
                    UUID(call[key])
                for key in ("started_at", "finished_at"):
                    if call[key] is not None:
                        if len(call[key]) > 40 or datetime.fromisoformat(call[key]).tzinfo is None:
                            raise ValueError
                if call["started_at"] is None:
                    raise ValueError
                for key in ("latency_ms", "input_tokens", "output_tokens"):
                    if call[key] is not None and (type(call[key]) is not int or call[key] < 0):
                        raise ValueError
                if call["outcome"] not in {
                    "running",
                    "completed",
                    "failed",
                    "interrupted",
                    "cancelled",
                    "timeout",
                }:
                    raise ValueError
                for key, limit in (("model", 200), ("error_code", 80)):
                    if not isinstance(call[key], str) or len(call[key]) > limit:
                        raise ValueError
                request_id = call["provider_request_id"]
                if request_id is not None and (
                    not isinstance(request_id, str)
                    or not 0 < len(request_id) <= 200
                    or not all(c.isascii() and (c.isalnum() or c in "_-") for c in request_id)
                ):
                    raise ValueError
                # Unknown future fields can never leak into templates.
                calls.append({key: call[key] for key in fields})
            return calls
        except httpx.HTTPError as exc:
            raise GatewayError("agent_metadata_unavailable") from exc
        except (ValueError, TypeError, AttributeError) as exc:
            raise GatewayError("agent_invalid_metadata") from exc
