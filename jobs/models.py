import secrets
import uuid

from django.conf import settings
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVector, SearchVectorField
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone


def private_path(instance, filename):
    return f"documents/{instance.owner_id}/{uuid.uuid4().hex}.pdf"


class Invitation(models.Model):
    email = models.EmailField(unique=True)
    token = models.CharField(
        max_length=128, default=secrets.token_urlsafe, unique=True, editable=False
    )
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return self.email


class Source(models.Model):
    class Adapter(models.TextChoices):
        GREENHOUSE = "greenhouse", "Greenhouse public jobs API"
        ASHBY = "ashby", "Ashby public posting API"
        RSS = "rss", "RSS / Atom"
        SECRET = "secret_telaviv", "Secret Tel Aviv (public HTML)"
        TELFED = "telfed", "Telfed (public HTML)"
        JSONLD = "jsonld", "Company page (JobPosting JSON-LD)"
        COMEET = "comeet", "Comeet public positions API"
        REALWORK = "realwork", "Real Work From Anywhere RSS + details"
        FORTYSEAS = "40seas", "40Seas careers"
        AGREMATCH = "agrematch", "Agrematch careers"
        AI21 = "ai21", "AI21 careers"
        AKEYLESS = "akeyless", "Akeyless / Comeet career links"
        AUDIOCODES = "audiocodes", "AudioCodes careers"

    discovered_from = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="discovered_sources"
    )
    discovery_url = models.URLField(max_length=2048, blank=True)
    discovery_pending = models.BooleanField(default=False)
    ai_normalization = models.BooleanField(default=False)
    employer_name = models.CharField(max_length=250, blank=True)
    authoritative = models.BooleanField(
        default=False,
        help_text="Reviewed employer source; complete ATS snapshots may close vacancies.",
    )
    name = models.CharField(max_length=160, unique=True)
    adapter = models.CharField(max_length=20, choices=Adapter.choices)
    url = models.URLField(max_length=2048)
    notes = models.TextField(
        blank=True, db_default="", help_text="Adapter limitations and operator review notes."
    )
    enabled = models.BooleanField(default=False)
    interval_minutes = models.PositiveIntegerField(default=360, validators=[MinValueValidator(15)])
    max_items = models.PositiveIntegerField(
        default=100, validators=[MinValueValidator(1), MaxValueValidator(500)]
    )
    max_response_bytes = models.PositiveIntegerField(
        default=2 * 1024 * 1024,
        validators=[MinValueValidator(1), MaxValueValidator(16 * 1024 * 1024)],
    )
    next_dispatch_at = models.DateTimeField(default=timezone.now, db_index=True)
    last_started_at = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    next_run_at = models.DateTimeField(default=timezone.now, db_index=True)
    failure_count = models.PositiveIntegerField(default=0)

    def clean(self):
        super().clean()
        if self.discovery_pending and (self.enabled or self.authoritative):
            raise ValidationError(
                "Review the employer relationship and clear discovery pending before enabling."
            )

    def __str__(self):
        return self.name


class IngestionRun(models.Model):
    source = models.ForeignKey(Source, on_delete=models.CASCADE)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True)
    imported = models.PositiveIntegerField(default=0)
    rejected = models.PositiveIntegerField(default=0)
    enrichment_failed = models.PositiveIntegerField(default=0, db_default=0)
    status = models.CharField(max_length=20, default="running")
    error_code = models.CharField(max_length=80, blank=True)


class Company(models.Model):
    # Source-scoped identity until an operator has confirmed the employer's identity.
    identity_key = models.CharField(max_length=64, unique=True, editable=False)
    name = models.CharField(max_length=250, db_index=True)
    source = models.ForeignKey(Source, null=True, blank=True, on_delete=models.SET_NULL)
    website = models.URLField(max_length=2048, blank=True)
    website_verified = models.BooleanField(default=False)
    description = models.TextField(blank=True)
    logo = models.FileField(upload_to="companies/logos/", blank=True)
    logo_source_url = models.URLField(max_length=2048, blank=True)
    enriched_at = models.DateTimeField(null=True, blank=True)
    enrichment_attempted_at = models.DateTimeField(null=True, blank=True)
    enrichment_error = models.CharField(max_length=80, blank=True)

    class Meta:
        ordering = ["name", "pk"]

    def __str__(self):
        return self.name

    @property
    def filter_label(self):
        from urllib.parse import urlsplit

        context = urlsplit(self.website).hostname if self.website_verified else None
        context = context or (self.source.name if self.source_id else "")
        return f"{self.name} · {context}" if context else self.name


class Job(models.Model):
    provider_key = models.CharField(max_length=300, unique=True, null=True, blank=True)
    employer_source = models.ForeignKey(
        Source, null=True, blank=True, on_delete=models.SET_NULL, related_name="verified_jobs"
    )
    verification = models.CharField(max_length=20, default="unverified")
    verified_at = models.DateTimeField(null=True, blank=True)
    canonical_url = models.URLField(max_length=2048, unique=True)
    title = models.CharField(max_length=300)
    company = models.CharField(max_length=250, blank=True)
    company_profile = models.ForeignKey(
        Company, related_name="jobs", null=True, blank=True, on_delete=models.SET_NULL
    )
    location = models.CharField(max_length=250, blank=True, db_index=True)
    description = models.TextField(blank=True)
    normalized = models.JSONField(default=dict, blank=True)
    apply_url = models.URLField(max_length=2048, blank=True)
    portal_access = models.CharField(
        max_length=20,
        default="unknown",
        choices=[
            ("unknown", "Check employer instructions"),
            ("public", "Public form"),
            ("login", "Sign-in required"),
            ("registration", "Registration required"),
        ],
    )
    apply_email = models.EmailField(blank=True)
    email_source = models.ForeignKey(
        Source, null=True, blank=True, on_delete=models.SET_NULL, related_name="email_jobs"
    )
    posted_at = models.DateTimeField(null=True, blank=True)
    discovered_at = models.DateTimeField(auto_now_add=True, db_index=True)
    last_seen_at = models.DateTimeField(default=timezone.now)
    active = models.BooleanField(default=True, db_index=True)
    search_document = models.GeneratedField(
        expression=SearchVector("title", "company", "description", config="english"),
        output_field=SearchVectorField(),
        db_persist=True,
    )

    class Meta:
        ordering = ["-discovered_at", "-pk"]
        indexes = [GinIndex(fields=["search_document"], name="job_search_gin")]

    def __str__(self):
        return self.title


class JobOrigin(models.Model):
    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="origins")
    source = models.ForeignKey(Source, on_delete=models.CASCADE)
    last_published_at = models.DateTimeField(null=True, blank=True)
    repost_count = models.PositiveIntegerField(default=0)
    source_url = models.URLField(max_length=2048)
    last_seen_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["job", "source"], name="unique_job_source")]


class SavedJob(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    job = models.ForeignKey(Job, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["owner", "job"], name="unique_saved_job")]


class Document(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    name = models.CharField(max_length=150)
    file = models.FileField(upload_to=private_path)
    sha256 = models.CharField(max_length=64, editable=False)
    size = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)


class GmailConnection(models.Model):
    owner = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    email = models.EmailField()
    encrypted_credentials = models.TextField()
    connected_at = models.DateTimeField(auto_now=True)
    revoked = models.BooleanField(default=False)


class Application(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Prepared"
        APPROVED = "approved", "Approved — queued"
        SUBMITTING = "submitting", "Submitting"
        CONFIRMED = "confirmed", "Confirmed sent"
        FAILED = "failed", "Failed — review required"
        UNKNOWN = "unknown", "Outcome unknown — do not resend"

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    job = models.ForeignKey(Job, on_delete=models.PROTECT)
    channel = models.CharField(
        max_length=20, default="email", choices=[("email", "Email"), ("portal", "Employer portal")]
    )
    portal_access = models.CharField(max_length=20, default="unknown")
    portal_url = models.URLField(max_length=2048, blank=True)
    portal_answers = models.JSONField(default=dict, blank=True)
    portal_fingerprint = models.CharField(max_length=64, blank=True)
    portal_receipt_url = models.URLField(max_length=2048, blank=True)
    recipient = models.EmailField(blank=True)
    subject = models.CharField(max_length=250)
    body = models.TextField(max_length=20000)
    document = models.ForeignKey(Document, on_delete=models.PROTECT, null=True, blank=True)
    notes = models.TextField(max_length=10000, blank=True)
    tracking_status = models.CharField(
        max_length=20,
        choices=[
            ("interested", "Interested"),
            ("applied", "Applied"),
            ("in_process", "In process"),
            ("ghosted", "Ghosted"),
            ("rejected", "Rejected"),
            ("withdrawn", "Withdrawn"),
            ("interview", "Interview"),
            ("offer", "Offer"),
            ("closed", "Closed"),
        ],
        default="interested",
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.DRAFT, db_index=True
    )
    next_dispatch_at = models.DateTimeField(default=timezone.now, db_index=True)
    send_attempts = models.PositiveIntegerField(default=0)
    retry_not_before = models.DateTimeField(null=True, blank=True)
    approval_digest = models.CharField(max_length=64, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    message_id = models.CharField(max_length=200, default="", blank=True)
    provider_id = models.CharField(max_length=200, blank=True)
    error_code = models.CharField(max_length=80, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["owner", "job"], name="unique_user_application")
        ]


class AuditEvent(models.Model):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    application = models.ForeignKey(Application, null=True, on_delete=models.SET_NULL)
    details = models.JSONField(default=dict, blank=True)
    event = models.CharField(max_length=80)
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def label(self):
        if self.event.startswith("tracking."):
            status = self.event.split(".", 1)[1]
            labels = dict(Application._meta.get_field("tracking_status").choices)
            return f"Tracking changed to {labels.get(status, status)}"
        return {
            "document.uploaded": "Base CV uploaded",
            "profile.confirmed": "Factual CV profile confirmed",
            "job.saved": "Job saved",
            "job.unsaved": "Job removed from saved list",
            "batch.created": "Application batch created",
            "gmail.connected": "Gmail connected",
            "gmail.disconnected": "Gmail disconnected",
            "account.registered": "Account created",
            "application.created": "Application drafted",
            "application.batch_prepared": "Application prepared for review",
            "application.approved": "Approved for sending",
            "application.edited": "Draft edited; approval cleared",
            "application.submitting": "Sending application",
            "application.confirmed": "Application sent",
            "application.failed": "Delivery failed",
            "application.unknown": "Delivery outcome needs checking",
            "application.preflight_failed": "Application checks failed before sending",
            "application.rate_limit_pause_requires_review": "Provider pause needs manual review",
            "application.rate_limit_retry_scheduled": "Rate limit reached; retry scheduled",
            "application.rate_limit_retries_exhausted": "Rate-limit retry budget exhausted",
            "application.batch_cancelled": "Pending delivery cancelled",
        }.get(self.event, "Activity recorded")


class ApplicationBatch(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    document = models.ForeignKey(Document, on_delete=models.PROTECT)
    profile = models.ForeignKey("ResumeProfile", null=True, blank=True, on_delete=models.PROTECT)
    document_sha256 = models.CharField(max_length=64, blank=True)
    portal_answers = models.JSONField(default=dict, blank=True)
    use_ai = models.BooleanField(default=False)
    request_key = models.UUIDField()
    cancelled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["owner", "request_key"], name="unique_batch_request")
        ]


class BatchItem(models.Model):
    batch = models.ForeignKey(ApplicationBatch, related_name="items", on_delete=models.CASCADE)
    job = models.ForeignKey(Job, on_delete=models.PROTECT)
    application = models.ForeignKey(Application, null=True, blank=True, on_delete=models.PROTECT)
    job_snapshot = models.JSONField(default=dict)
    provenance = models.JSONField(default=dict)
    lease_token = models.UUIDField(null=True, blank=True)
    lease_until = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    state = models.CharField(max_length=24, default="queued", db_index=True)
    error_code = models.CharField(max_length=80, blank=True)
    next_attempt_at = models.DateTimeField(default=timezone.now, db_index=True)
    next_dispatch_at = models.DateTimeField(default=timezone.now, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["batch", "job"], name="unique_batch_job")]

    @property
    def state_label(self):
        return {
            "queued": "Queued",
            "preparing": "Preparing",
            "ready": "Ready for review",
            "existing": "Already tracked",
            "needs_action": "Needs your action",
            "approved": "Approved",
            "cancelled": "Cancelled",
            "failed": "Failed",
        }.get(self.state, self.state)

    @property
    def error_message(self):
        if not self.error_code:
            return ""
        return {
            "secret_telaviv_details_required": "Add applicant details and review terms.",
            "portal_rate_limited": "Secret Tel Aviv requested a pause; retry scheduled.",
            "portal_form_changed": "The employer form changed; review it before applying.",
            "portal_terms_changed": "The employer terms changed and need review.",
            "manual_portal_required": "Use the prepared materials to apply on the employer site.",
            "vacancy_inactive": "This vacancy is no longer open.",
            "cv_validation_failed": "Review your base CV, confirmed facts, and font support.",
            "preparation_unavailable": "Preparation is unavailable; a retry is scheduled.",
            "preparation_retries_exhausted": "Preparation could not finish after three attempts.",
            "ai_rate_limited": "The AI provider requested a pause; a retry is scheduled.",
            "ai_retry_scheduled": "Waiting for the AI provider retry window.",
            "ai_daily_budget_exceeded": "The configured daily AI budget has been reached.",
            "ai_not_configured": "OpenAI needs to be configured before using AI preparation.",
            "ai_unsupported_claim": "AI output could not be supported by your confirmed facts.",
            "ai_input_budget_exceeded": "The profile and job exceed the configured AI input limit.",
        }.get(
            self.error_code, "Preparation needs attention. Review the item or contact the operator."
        )


class ResumeProfile(models.Model):
    """Immutable version of candidate-confirmed facts, including additions to the PDF."""

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    document = models.ForeignKey(Document, on_delete=models.PROTECT, related_name="profiles")
    name = models.CharField(max_length=150)
    contact = models.CharField(max_length=500, blank=True)
    facts = models.JSONField(default=list)
    confirmed_at = models.DateTimeField(default=timezone.now)


class DeliveryThrottle(models.Model):
    key = models.CharField(max_length=80, unique=True)
    available_at = models.DateTimeField(default=timezone.now)


class AIResult(models.Model):
    key = models.CharField(max_length=64, unique=True)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.CASCADE)
    result = models.JSONField(default=dict)
    lease_until = models.DateTimeField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)


class AIQuota(models.Model):
    day = models.DateField(unique=True)
    reserved_tokens = models.PositiveIntegerField(default=0)


class RecruitingContact(models.Model):
    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="recruiting_contacts")
    source = models.ForeignKey(Source, on_delete=models.CASCADE)
    email = models.EmailField()
    purpose = models.CharField(max_length=20, default="recruiting")
    evidence = models.TextField(max_length=1000)
    source_url = models.URLField(max_length=2048)
    last_seen_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["job", "source", "email"], name="unique_recruiting_contact"
            )
        ]


class PreparedResume(models.Model):
    """Private immutable preparation reused for identical profile/job/rendering inputs."""

    key = models.CharField(max_length=64, unique=True)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    profile = models.ForeignKey(ResumeProfile, on_delete=models.PROTECT)
    document = models.ForeignKey(
        Document, on_delete=models.PROTECT, related_name="prepared_resumes"
    )
    provenance = models.JSONField(default=dict)
    body = models.TextField(max_length=20000)
    created_at = models.DateTimeField(auto_now_add=True)
