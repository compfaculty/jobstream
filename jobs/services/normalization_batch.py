"""Durable, public-data-only Batch normalization with conservative ambiguity handling."""

import hashlib
import json
import re
import time
from datetime import timedelta

import httpx
from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

from jobs.models import Job, NormalizationBatch, NormalizationTarget, NormalizationWork

from .ai import AIUnavailable
from .ai_costs import BudgetUnavailable, reserve, settle
from .normalization import (
    NORMALIZATION_PROMPT,
    VERSION,
    apply_ai_sections,
    needs_ai_normalization,
    normalize_description,
    section_schema,
    text_blocks,
)

TERMINAL = {"completed", "failed", "expired", "cancelled"}


def enqueue(job):
    if {"description", "normalized"} & set(job.content_overrides) or not needs_ai_normalization(
        job.normalized
    ):
        return None
    signature = [
        job.description,
        VERSION,
        NORMALIZATION_PROMPT,
        section_schema(),
        settings.JOB_NORMALIZATION_MODEL,
        settings.JOB_NORMALIZATION_REASONING_EFFORT,
        settings.JOB_NORMALIZATION_MAX_OUTPUT_TOKENS,
    ]
    key = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()
    work, _ = NormalizationWork.objects.get_or_create(
        key=key,
        defaults={
            "description": job.description,
            "model": settings.JOB_NORMALIZATION_MODEL,
            "request": build_body(job.description, settings.JOB_NORMALIZATION_MODEL),
        },
    )
    NormalizationTarget.objects.get_or_create(work=work, job=job)
    if work.state == "completed":
        apply_result(work)
    return work


def request_body(work):
    return work.request or build_body(work.description, work.model)


def build_body(description, model):
    return {
        "model": model,
        "store": False,
        "instructions": NORMALIZATION_PROMPT,
        "input": json.dumps(
            {"lines": list(enumerate(text_blocks(description).splitlines()))},
            ensure_ascii=False,
        ),
        "reasoning": {"effort": settings.JOB_NORMALIZATION_REASONING_EFFORT},
        "max_output_tokens": settings.JOB_NORMALIZATION_MAX_OUTPUT_TOKENS,
        "text": {
            "format": {
                "type": "json_schema",
                "name": "job_sections",
                "strict": True,
                "schema": section_schema(),
            }
        },
    }


def api(method, path, *, content=False, **kwargs):
    if not settings.OPENAI_API_KEY:
        raise AIUnavailable("ai_not_configured")
    # Provider IDs and paths never originate from document or job text.
    if not re.fullmatch(r"/(?:batches|files)(?:/[A-Za-z0-9_-]+)?(?:/content)?", path):
        raise AIUnavailable("ai_invalid_provider_id")
    started = time.monotonic()
    try:
        with httpx.Client(timeout=20, follow_redirects=False, trust_env=False) as client:
            with client.stream(
                method,
                "https://api.openai.com/v1" + path,
                headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}"},
                **kwargs,
            ) as response:
                if response.status_code >= 400:
                    raise AIUnavailable("ai_batch_http_error")
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    if time.monotonic() - started > 20:
                        raise AIUnavailable("ai_batch_timeout")
                    size += len(chunk)
                    if size > 8 * 1024 * 1024:
                        raise AIUnavailable("ai_batch_response_too_large")
                    chunks.append(chunk)
                raw = b"".join(chunks)
                return raw.decode() if content else json.loads(raw)
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise AIUnavailable("ai_batch_unavailable") from exc


def prepare():
    with transaction.atomic():
        items = list(
            NormalizationWork.objects.select_for_update(skip_locked=True)
            .filter(
                state__in=["pending", "budget_blocked", "retry"],
                available_at__lte=timezone.now(),
                attempts__lt=3,
            )
            .order_by("pk")[: settings.JOB_NORMALIZATION_BATCH_SIZE]
        )
        if not items:
            return None
        batch = NormalizationBatch.objects.create()
        for item in items:
            body = request_body(item)
            if len(body["input"]) > settings.AI_MAX_INPUT_CHARACTERS:
                item.state, item.error_code = "failed", "ai_input_budget_exceeded"
                item.save(update_fields=["state", "error_code"])
                continue
            try:
                charge = reserve(
                    f"batch:{item.key}:{item.attempts + 1}",
                    item.model,
                    "normalization",
                    len(json.dumps(body).encode()) + 1024,
                    settings.JOB_NORMALIZATION_MAX_OUTPUT_TOKENS,
                    batch=True,
                    daily=True,
                )
            except BudgetUnavailable as exc:
                item.state, item.error_code = "budget_blocked", exc.code
                item.available_at = timezone.now() + timedelta(hours=1)
            else:
                item.state, item.batch, item.charge = "prepared", batch, charge
                item.attempts += 1
            item.save()
        if not batch.normalizationwork_set.exists():
            batch.delete()
            return None
        return batch


def submit(batch):
    # Fence network publication; discover ambiguous submissions without retrying create.
    with transaction.atomic():
        batch = NormalizationBatch.objects.select_for_update().get(pk=batch.pk)
        if batch.state != "prepared":
            return
        batch.state, batch.lease_until = "submitting", timezone.now() + timedelta(minutes=3)
        batch.save()
    try:
        lines = [
            json.dumps(
                {
                    "custom_id": item.key,
                    "method": "POST",
                    "url": "/v1/responses",
                    "body": request_body(item),
                }
            )
            for item in batch.normalizationwork_set.order_by("pk")
        ]
        upload = api(
            "POST",
            "/files",
            data={"purpose": "batch"},
            files={"file": ("normalization.jsonl", "\n".join(lines).encode(), "application/jsonl")},
        )
        batch.input_file = upload["id"]
        batch.save(update_fields=["input_file"])
        result = api(
            "POST",
            "/batches",
            json={
                "input_file_id": batch.input_file,
                "endpoint": "/v1/responses",
                "completion_window": "24h",
                "metadata": {"jobstream_batch": str(batch.pk)},
            },
        )
        batch.provider_id, batch.state, batch.lease_until = result["id"], result["status"], None
        batch.save()
        batch.normalizationwork_set.update(state="submitted")
    except AIUnavailable, KeyError, TypeError:
        NormalizationBatch.objects.filter(pk=batch.pk).update(
            state="unknown", error_code="ai_batch_submission_unknown", lease_until=None
        )


def apply_result(work):
    for pk in work.targets.values_list("job_id", flat=True).iterator():
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock(720316003)")
            job = Job.objects.select_for_update().filter(pk=pk).first()
            if (
                not job
                or job.description != work.description
                or {"description", "normalized"} & set(job.content_overrides)
            ):
                continue
            job.normalized = work.result
            imported = dict(job.imported_content)
            if imported.get("description") == job.description:
                imported["normalized"] = work.result
                job.imported_content = imported
            job.save(update_fields=["normalized", "imported_content"])


def consume(batch, raw):
    for line in raw.splitlines():
        try:
            row = json.loads(line)
            with transaction.atomic():
                item = (
                    NormalizationWork.objects.select_for_update()
                    .filter(batch=batch, key=row.get("custom_id"))
                    .first()
                )
                if item is None or item.state in {"completed", "failed", "retry"}:
                    continue
                response = row.get("response") or {}
                body = response.get("body") or {}
                usage = body.get("usage")
                if usage:
                    settle(item.charge_id, usage)
                if response.get("status_code") != 200:
                    # Refund only failures that prove inference never started.
                    if not usage and (
                        response.get("status_code") in {400, 401, 403, 404, 429}
                        or (row.get("error") or {}).get("code") == "batch_expired"
                    ):
                        settle(item.charge_id, {"input_tokens": 0, "output_tokens": 0})
                    item.state = "retry" if item.attempts < 3 else "failed"
                    item.error_code = "ai_batch_item_failed"
                    item.available_at = timezone.now() + timedelta(hours=1)
                else:
                    if body.get("status") != "completed":
                        raise AIUnavailable("ai_incomplete_response")
                    texts = [
                        p["text"]
                        for out in body.get("output", [])
                        if out.get("type") == "message"
                        for p in out.get("content", [])
                        if p.get("type") == "output_text"
                    ]
                    selected = json.loads("".join(texts))
                    item.result = apply_ai_sections(
                        item.description,
                        normalize_description(item.description),
                        selected,
                        item.model,
                    )
                    item.state, item.error_code = "completed", ""
                item.save()
            if item.state == "completed":
                apply_result(item)
        except ValueError, KeyError, TypeError, AIUnavailable:
            # Invalid or ungrounded outputs cannot change a job. Keep uncertain usage reserved.
            key = row.get("custom_id") if isinstance(locals().get("row"), dict) else None
            NormalizationWork.objects.filter(batch=batch, key=key).exclude(
                state="completed"
            ).update(state="failed", error_code="ai_batch_invalid_result")


def poll(batch):
    with transaction.atomic():
        batch = NormalizationBatch.objects.select_for_update().get(pk=batch.pk)
        if batch.state in TERMINAL or (batch.lease_until and batch.lease_until > timezone.now()):
            return
        batch.lease_until = timezone.now() + timedelta(minutes=3)
        batch.save(update_fields=["lease_until"])
    try:
        if not batch.provider_id:
            # Reconcile a lost create response using our durable metadata. No create retry.
            listed = api("GET", "/batches", params={"limit": 100})
            match = next(
                (
                    r
                    for r in listed.get("data", [])
                    if r.get("metadata", {}).get("jobstream_batch") == str(batch.pk)
                ),
                None,
            )
            if not match:
                return
            batch.provider_id = match["id"]
        result = api("GET", f"/batches/{batch.provider_id}")
        status = result["status"]
        if status in TERMINAL:
            for field in ("output_file_id", "error_file_id"):
                if result.get(field):
                    consume(batch, api("GET", f"/files/{result[field]}/content", content=True))
            # Missing terminal results retain their reservation: usage is unknown.
            remaining = batch.normalizationwork_set.exclude(
                state__in=["completed", "failed", "retry"]
            )
            for item in remaining:
                item.state = "failed"
                item.error_code = "ai_batch_missing_result"
                item.save(update_fields=["state", "error_code"])
            for item in batch.normalizationwork_set.filter(state="completed"):
                apply_result(item)
        batch.state, batch.error_code = status, ""
        batch.save(update_fields=["provider_id", "state", "error_code", "updated_at"])
    except AIUnavailable, KeyError, TypeError:
        NormalizationBatch.objects.filter(pk=batch.pk).update(
            error_code="ai_batch_poll_unavailable"
        )
    finally:
        NormalizationBatch.objects.filter(pk=batch.pk).update(lease_until=None)
