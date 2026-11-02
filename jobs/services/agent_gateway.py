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
    def parse(cls, data: object, execution_id: str, attempt_id: str):
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
    def call(self, method: str, execution_id: str, attempt_id: str, payload=None):
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

    def start(self, request: ExecutionRequest):
        from dataclasses import asdict

        return self.call("PUT", request.execution_id, request.attempt_id, asdict(request))

    def status(self, execution_id: str, attempt_id: str):
        return self.call("GET", execution_id, attempt_id)

    def cancel(self, execution_id: str, attempt_id: str):
        return self.call("DELETE", execution_id, attempt_id)
