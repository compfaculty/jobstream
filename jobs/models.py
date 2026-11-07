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
    class Kind(models.TextChoices):
        COMPANY = "company", "Company careers"
        BOARD = "board", "Job board"

    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.BOARD)

    class Adapter(models.TextChoices):
        WORKDAY = "workday", "Workday public careers"
        GREENHOUSE = "greenhouse", "Greenhouse public jobs API"
        ASHBY = "ashby", "Ashby public posting API"
        RSS = "rss", "RSS / Atom"
        SECRET = "secret_telaviv", "Secret Tel Aviv (public HTML)"
        TELFED = "telfed", "Telfed (public HTML)"
        JSONLD = "jsonld", "Company page (JobPosting JSON-LD)"
        COMEET = "comeet", "Comeet public positions API"
        REALWORK = "realwork", "Real Work From Anywhere RSS + details"
        ARBEITNOW = "arbeitnow", "Arbeitnow job board"
        HIMALAYAS = "himalayas", "Himalayas remote jobs"
        FORTYSEAS = "40seas", "40Seas careers"
        AGREMATCH = "agrematch", "Agrematch careers"
        AI21 = "ai21", "AI21 careers"
        AKEYLESS = "akeyless", "Akeyless / Comeet career links"
        AUDIOCODES = "audiocodes", "AudioCodes careers"

    company = models.ForeignKey(
        "Company", null=True, blank=True, on_delete=models.PROTECT, related_name="career_sources"
    )
    closure_enabled = models.BooleanField(
        default=False, help_text="Enable after shadow validation."
    )
    last_complete_at = models.DateTimeField(null=True, blank=True)
    inventory_token = models.UUIDField(null=True, blank=True, editable=False)
    inventory_cursor = models.JSONField(default=dict, blank=True, editable=False)
    inventory_config = models.CharField(max_length=64, blank=True, editable=False)
    inventory_started_at = models.DateTimeField(null=True, blank=True, editable=False)
    lease_token = models.UUIDField(null=True, blank=True, editable=False)
    lease_until = models.DateTimeField(null=True, blank=True, editable=False, db_index=True)
    last_error = models.CharField(max_length=80, blank=True)
    review_reason = models.CharField(max_length=250, blank=True)
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
    name = models.CharField(max_length=160)
    adapter = models.CharField(max_length=20, choices=Adapter.choices)
    url = models.URLField(max_length=2048, unique=True)
    notes = models.TextField(
        blank=True, db_default="", help_text="Adapter limitations and operator review notes."
    )
    enabled = models.BooleanField(default=False)
    interval_minutes = models.PositiveIntegerField(default=1440, validators=[MinValueValidator(15)])
    max_items = models.PositiveIntegerField(
        default=100000, validators=[MinValueValidator(1), MaxValueValidator(100000)]
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

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(
                        kind="board",
                        company__isnull=True,
                        authoritative=False,
                        closure_enabled=False,
                    )
                    | models.Q(kind="company", company__isnull=False)
                    | models.Q(
                        kind="company", enabled=False, authoritative=False, closure_enabled=False
                    )
                ),
                name="source_kind_ownership",
            ),
        ]
        indexes = [
            models.Index(
                fields=["next_run_at", "next_dispatch_at"],
                condition=models.Q(enabled=True, discovery_pending=False),
                name="source_due_enabled",
            )
        ]

    def clean(self):
        super().clean()
        from .services.network import canonical_url

        try:
            canonical_url(self.url)
        except ValueError as exc:
            raise ValidationError({"url": str(exc)}) from exc
        if self.kind == self.Kind.BOARD and (
            self.company_id or self.authoritative or self.closure_enabled
        ):
            raise ValidationError(
                "Job boards cannot own a Company or authoritative employer state."
            )
        if self.kind == self.Kind.COMPANY and self.enabled and not self.company_id:
            raise ValidationError("Assign the company before enabling its career source.")
        if self.closure_enabled and not self.authoritative:
            raise ValidationError("Only authoritative sources can enable automatic closure.")
        if self.authoritative and self.company_id and not self.company.website_verified:
            raise ValidationError("Verify the official company website before granting authority.")
        if self.adapter == "workday":
            from .services.inventories import workday_site

            try:
                workday_site(self.url)
            except ValueError as exc:
                raise ValidationError({"url": str(exc)}) from exc
        if self.discovery_pending and (self.enabled or self.authoritative):
            raise ValidationError(
                "Review the employer relationship and clear discovery pending before enabling."
            )

    def __str__(self):
        return self.name


class IngestionRun(models.Model):
    source = models.ForeignKey(Source, on_delete=models.CASCADE)
    started_at = models.DateTimeField(auto_now_add=True, db_index=True)
    finished_at = models.DateTimeField(null=True)
    imported = models.PositiveIntegerField(default=0)
    rejected = models.PositiveIntegerField(default=0)
    enrichment_failed = models.PositiveIntegerField(default=0, db_default=0)
    status = models.CharField(max_length=20, default="running")
    error_code = models.CharField(max_length=80, blank=True)
    complete = models.BooleanField(default=False)
    scope = models.CharField(max_length=300, blank=True)
    request_count = models.PositiveIntegerField(default=0)
    queue_delay_seconds = models.FloatField(default=0)
    duration_seconds = models.FloatField(default=0)
    would_close = models.PositiveIntegerField(default=0)


class Company(models.Model):
    # Unverified identities remain distinct; verified official domains can be consolidated.
    identity_key = models.CharField(
        max_length=64, unique=True, editable=False, default=secrets.token_hex
    )
    name = models.CharField(max_length=250, db_index=True)
    website = models.URLField(max_length=2048, blank=True)
    website_verified = models.BooleanField(default=False)
    official_domain = models.CharField(max_length=253, null=True, blank=True, unique=True)
    review_required = models.BooleanField(default=True)
    discovery_enabled = models.BooleanField(default=True)
    next_discovery_at = models.DateTimeField(default=timezone.now, db_index=True)
    discovery_error = models.CharField(max_length=80, blank=True)
    description = models.TextField(blank=True)
    logo = models.FileField(upload_to="companies/logos/", blank=True)
    logo_source_url = models.URLField(max_length=2048, blank=True)
    enriched_at = models.DateTimeField(null=True, blank=True)
    enrichment_attempted_at = models.DateTimeField(null=True, blank=True)
    enrichment_error = models.CharField(max_length=80, blank=True)

    class Meta:
        ordering = ["name", "pk"]
        verbose_name_plural = "Companies"

    def clean(self):
        super().clean()
        if self.website:
            from .services.network import canonical_url

            try:
                canonical_url(self.website)
            except ValueError as exc:
                raise ValidationError({"website": str(exc)}) from exc

    def __str__(self):
        return self.name

    @property
    def filter_label(self):
        from urllib.parse import urlsplit

        context = urlsplit(self.website).hostname if self.website_verified else None
        return f"{self.name} · {context}" if context else self.name


class CompanyAlias(models.Model):
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="aliases")
    name = models.CharField(max_length=250)
    legacy_identity = models.CharField(max_length=64, unique=True, null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["company", "name"], name="company_alias_name")
        ]


class CrawlLease(models.Model):
    token = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    host = models.CharField(max_length=253, db_index=True)
    expires_at = models.DateTimeField(db_index=True)


class CrawlHost(models.Model):
    host = models.CharField(max_length=253, primary_key=True)
    available_at = models.DateTimeField(default=timezone.now)


class Job(models.Model):
    class Lifecycle(models.TextChoices):
        OPEN = "open", "Open"
        CLOSED = "closed", "Closed"
        FILLED = "filled", "Filled (employer confirmed)"
        ARCHIVED = "archived", "Archived"

    lifecycle = models.CharField(
        max_length=20, choices=Lifecycle.choices, default="open", db_index=True
    )
    status_override = models.CharField(max_length=20, choices=Lifecycle.choices, blank=True)
    content_overrides = models.JSONField(default=dict, blank=True)
    imported_content = models.JSONField(default=dict, blank=True, editable=False)
    status_reason = models.CharField(max_length=100, blank=True)
    status_changed_at = models.DateTimeField(default=timezone.now)

    provider_key = models.CharField(max_length=300, unique=True, null=True, blank=True)
    employer_source = models.ForeignKey(
        Source, null=True, blank=True, on_delete=models.SET_NULL, related_name="verified_jobs"
    )
    verification = models.CharField(max_length=20, default="unverified")
    verified_at = models.DateTimeField(null=True, blank=True)
    canonical_url = models.URLField(max_length=2048, unique=True)
    title = models.CharField(max_length=300)
    company = models.CharField("Employer name from posting", max_length=250, blank=True)
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
        indexes = [
            GinIndex(fields=["search_document"], name="job_search_gin"),
            GinIndex(fields=["normalized"], name="job_normalized_gin"),
        ]

    @classmethod
    def from_db(cls, db, field_names, values):
        instance = super().from_db(db, field_names, values)
        instance._original_active = instance.__dict__.get("active")
        instance._original_lifecycle = instance.__dict__.get("lifecycle")
        return instance

    def save(self, *args, **kwargs):
        if self._state.adding and (not self.active or self.verification == "closed"):
            if self.lifecycle == self.Lifecycle.OPEN:
                self.lifecycle = self.Lifecycle.CLOSED
        # Compatibility for callers explicitly assigning the old public boolean.
        previous = getattr(self, "_original_active", None)
        if (
            previous is not None
            and self.active != previous
            and not self.status_override
            and self.lifecycle == getattr(self, "_original_lifecycle", None)
        ):
            self.lifecycle = self.Lifecycle.OPEN if self.active else self.Lifecycle.CLOSED
            if kwargs.get("update_fields") is not None:
                kwargs["update_fields"] = set(kwargs["update_fields"]) | {"lifecycle"}
        self.active = (self.status_override or self.lifecycle) == self.Lifecycle.OPEN
        if kwargs.get("update_fields") is not None:
            kwargs["update_fields"] = set(kwargs["update_fields"]) | {"active"}
        super().save(*args, **kwargs)
        self._original_active = self.active
        self._original_lifecycle = self.lifecycle

    def __str__(self):
        return self.title


class JobEvent(models.Model):
    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="lifecycle_events")
    source = models.ForeignKey(Source, null=True, blank=True, on_delete=models.SET_NULL)
    run = models.ForeignKey(IngestionRun, null=True, blank=True, on_delete=models.SET_NULL)
    status = models.CharField(max_length=20)
    reason = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)


class JobOrigin(models.Model):
    scope = models.CharField(max_length=300, blank=True)
    authoritative_observation = models.BooleanField(default=False)
    inventory_token = models.UUIDField(null=True, blank=True, editable=False)
    missing_count = models.PositiveIntegerField(default=0)
    observed_status = models.CharField(max_length=20, default="open")
    last_complete_at = models.DateTimeField(null=True, blank=True)
    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="origins")
    source = models.ForeignKey(Source, on_delete=models.CASCADE)
    last_published_at = models.DateTimeField(null=True, blank=True)
    repost_count = models.PositiveIntegerField(default=0)
    source_url = models.URLField(max_length=2048)
    last_seen_at = models.DateTimeField(default=timezone.now)

    class Meta:
        indexes = [
            models.Index(
                fields=["source", "scope", "inventory_token"], name="origin_inventory_scope"
            )
        ]
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

    @property
    def processing_status(self):
        record = self.representations.order_by("-created_at").first()
        return (
            {
                "pending": "Processing queued",
                "dispatching": "Processing queued",
                "processing": "Processing CV",
                "ready": "Ready for comparison",
                "fallback": "Ready using extracted PDF text",
                "failed": "Could not read PDF — upload a text-based PDF",
            }.get(record.state, "")
            if record
            else ""
        )


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
    assisted_delivery = models.BooleanField(default=False)
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
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
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


class ApplicationPreferences(models.Model):
    owner = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    excluded_technologies = models.JSONField(default=list, blank=True)
    wanted_technologies = models.JSONField(default=list, blank=True)
    preferred_locations = models.JSONField(default=list, blank=True)
    preferred_titles = models.JSONField(default=list, blank=True)
    location_required = models.BooleanField(default=False)
    title_required = models.BooleanField(default=False)
    technology_required = models.BooleanField(default=False)
    remote_only = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        super().clean()
        from .services.normalization import TECHNOLOGIES

        for field in ("excluded_technologies", "wanted_technologies"):
            values = getattr(self, field)
            if (
                not isinstance(values, list)
                or any(not isinstance(value, str) or value not in TECHNOLOGIES for value in values)
                or len(set(values)) != len(values)
            ):
                raise ValidationError({field: "Select supported unique technologies."})
        if set(self.excluded_technologies) & set(self.wanted_technologies):
            raise ValidationError("A technology cannot be both wanted and excluded.")
        for field in ("preferred_locations", "preferred_titles"):
            values = getattr(self, field)
            if (
                not isinstance(values, list)
                or len(values) > 30
                or any(
                    not isinstance(value, str) or not value.strip() or len(value) > 150
                    for value in values
                )
            ):
                raise ValidationError(
                    {field: "Enter at most 30 nonempty values, up to 150 characters each."}
                )
        for flag, field in (
            ("location_required", "preferred_locations"),
            ("title_required", "preferred_titles"),
            ("technology_required", "wanted_technologies"),
        ):
            if getattr(self, flag) and not getattr(self, field):
                raise ValidationError({field: "Add at least one value before enabling this limit."})


class SavedApplicationAnswers(models.Model):
    owner = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    encrypted_answers = models.TextField()
    reuse_enabled = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)


class ApplicationBatch(models.Model):
    check_fit = models.BooleanField(default=False)
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
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["batch", "job"], name="unique_batch_job")]

    @property
    def state_label(self):
        return {
            "skipped": "Skipped",
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
            "fit_location_unknown": "Review the job location before applying.",
            "fit_location_excluded": "Skipped: the location is outside your allowed locations.",
            "fit_title_excluded": "The job title does not match your allowed titles.",
            "fit_wanted_technology_missing": "The job does not list a wanted technology.",
            "fit_remote_required": "Skipped: your preferences require remote work.",
            "fit_workplace_unknown": "Review whether the job supports remote work.",
            "fit_excluded_technology": "Skipped: an explicitly excluded technology is mandatory.",
            "fit_no_technology_overlap": "Skipped: no CV technology overlaps the job requirements.",
            "fit_requirements_unknown": "Review the requirements or provide a readable CV/profile.",
            "fit_ambiguous_requirement": "Review whether an excluded technology is mandatory.",
            "fit_cv_needs_review": "Choose a reviewed CV profile or a readable PDF to check fit.",
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
    details = models.JSONField(default=dict, blank=True)
    facts = models.JSONField(default=list)
    confirmed_at = models.DateTimeField(default=timezone.now)


class DeliveryThrottle(models.Model):
    key = models.CharField(max_length=80, unique=True)
    available_at = models.DateTimeField(default=timezone.now)


class AIResult(models.Model):
    cache_hits = models.PositiveIntegerField(default=0)
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


class AgentExecution(models.Model):
    """Owner-scoped evaluation ledger; runtime history is stored separately."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    job = models.ForeignKey(Job, on_delete=models.CASCADE)
    profile = models.ForeignKey(ResumeProfile, on_delete=models.PROTECT, null=True, blank=True)
    document = models.ForeignKey(Document, on_delete=models.PROTECT, null=True, blank=True)
    key = models.CharField(max_length=64, unique=True)
    snapshot = models.JSONField()
    cache_hits = models.PositiveIntegerField(default=0)
    backend = models.CharField(max_length=20, default="infinity")
    model = models.CharField(max_length=200)
    version = models.CharField(max_length=40, default="vacancy-evaluation-v1")
    status = models.CharField(max_length=20, default="queued", db_index=True)
    attempt = models.PositiveSmallIntegerField(default=1)
    attempt_id = models.UUIDField(default=uuid.uuid4)
    result = models.JSONField(default=dict)
    usage = models.JSONField(default=dict)
    error_code = models.CharField(max_length=80, blank=True)
    retryable = models.BooleanField(default=False)
    cancellation_delivered = models.BooleanField(default=False)
    lease_until = models.DateTimeField(null=True, blank=True)
    next_dispatch_at = models.DateTimeField(default=timezone.now, db_index=True)
    deadline = models.DateTimeField(null=True, blank=True)
    budget_day = models.DateField(null=True, blank=True)
    budget_reserved_tokens = models.PositiveIntegerField(default=0)
    budget_charged_tokens = models.PositiveIntegerField(null=True, blank=True)
    budget_settled = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["owner", "document", "job", "-created_at"],
                condition=models.Q(status="completed"),
                name="agent_recommendation_idx",
            )
        ]

    @property
    def error_message(self):
        return {
            "ai_monthly_budget_exceeded": (
                "The app’s monthly AI budget has been reached. No new request was sent."
            ),
            "ai_user_daily_limit": (
                "You have used today’s paid comparison allowance. Cached results remain available."
            ),
            "ai_model_price_unknown": "AI pricing is not configured for this model.",
            "ai_daily_budget_exceeded": (
                "The app's daily AI budget has insufficient unreserved capacity. "
                "No OpenAI request was sent. Retry when capacity becomes available."
            ),
            "ai_evaluation_budget_too_small": (
                "This evaluation's safety allowance exceeds the configured daily AI budget. "
                "Choose a shorter CV or adjust the app's budget; waiting will not resolve this."
            ),
        }.get(self.error_code, self.error_code)


class OperationalSnapshot(models.Model):
    """Latest bounded metadata snapshot; no task arguments or provider content."""

    key = models.CharField(max_length=40, primary_key=True)
    observed_at = models.DateTimeField(default=timezone.now)
    payload = models.JSONField(default=dict)


class AICostMonth(models.Model):
    month = models.DateField(unique=True)
    committed_usd = models.DecimalField(max_digits=18, decimal_places=8, default=0)
    carried_usd = models.DecimalField(max_digits=18, decimal_places=8, default=0)


class AICharge(models.Model):
    key = models.CharField(max_length=180, unique=True)
    month = models.ForeignKey(AICostMonth, on_delete=models.PROTECT)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    workload = models.CharField(max_length=40, db_index=True)
    model = models.CharField(max_length=200)
    pricing = models.JSONField()
    reserved_usd = models.DecimalField(max_digits=18, decimal_places=8)
    charged_usd = models.DecimalField(max_digits=18, decimal_places=8, null=True)
    usage = models.JSONField(default=dict)
    state = models.CharField(max_length=20, default="reserved", db_index=True)
    daily_tokens = models.PositiveIntegerField(default=0)
    day = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)


class UserComparisonDay(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    day = models.DateField()
    admitted = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["owner", "day"], name="user_comparison_day")]


class CVRepresentation(models.Model):
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="representations")
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    key = models.CharField(max_length=64, unique=True)
    version = models.CharField(max_length=100)
    state = models.CharField(max_length=20, default="pending", db_index=True)
    text = models.TextField(blank=True)
    facts = models.JSONField(default=list)
    skills = models.JSONField(default=list)
    error_code = models.CharField(max_length=80, blank=True)
    lease_until = models.DateTimeField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class NormalizationBatch(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    provider_id = models.CharField(max_length=200, blank=True)
    input_file = models.CharField(max_length=200, blank=True)
    state = models.CharField(max_length=30, default="prepared", db_index=True)
    error_code = models.CharField(max_length=80, blank=True)
    lease_until = models.DateTimeField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class NormalizationWork(models.Model):
    request = models.JSONField(default=dict)
    key = models.CharField(max_length=64, unique=True)
    description = models.TextField()
    model = models.CharField(max_length=200)
    state = models.CharField(max_length=30, default="pending", db_index=True)
    result = models.JSONField(default=dict)
    error_code = models.CharField(max_length=80, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    batch = models.ForeignKey(NormalizationBatch, null=True, on_delete=models.SET_NULL)
    charge = models.ForeignKey(AICharge, null=True, on_delete=models.SET_NULL)
    available_at = models.DateTimeField(default=timezone.now, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)


class NormalizationTarget(models.Model):
    work = models.ForeignKey(NormalizationWork, on_delete=models.CASCADE, related_name="targets")
    job = models.ForeignKey(Job, on_delete=models.CASCADE)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["work", "job"], name="normalization_target")]


class EmployerDiscoveryCampaign(models.Model):
    provider = models.CharField(
        max_length=12,
        default="brave",
        choices=[
            ("brave", "Brave Search API"),
            ("pituchim", "Pituchim public directory"),
            ("arbeitnow", "Arbeitnow public feed"),
            ("himalayas", "Himalayas public feed"),
        ],
    )
    catalogue_mode = models.BooleanField(
        default=False, help_text="Import pending companies before checking vacancies."
    )
    hiring_only = models.BooleanField(
        default=True, help_text="Pituchim only: restrict company imports to Hiring badges."
    )
    directory_page = models.PositiveIntegerField(default=1, editable=False)
    name = models.CharField(max_length=200, unique=True)
    enabled = models.BooleanField(default=False)
    queries = models.JSONField(
        default=list, blank=True, help_text="Brave only: public search queries, up to 200 strings."
    )
    technology_roles = models.JSONField(
        default=list, help_text="Case-insensitive job title phrases."
    )
    daily_search_limit = models.PositiveSmallIntegerField(
        default=30, validators=[MinValueValidator(1), MaxValueValidator(500)]
    )
    daily_candidate_limit = models.PositiveSmallIntegerField(
        default=100, validators=[MinValueValidator(1), MaxValueValidator(100)]
    )
    next_run_at = models.DateTimeField(default=timezone.now, db_index=True)
    query_cursor = models.PositiveIntegerField(default=0, editable=False)
    query_pages = models.JSONField(default=dict, editable=False)

    def clean(self):
        super().clean()
        if self.provider == "brave" and self.catalogue_mode:
            raise ValidationError(
                {"catalogue_mode": "Brave retains its evidence-based lead workflow."}
            )
        if self.provider == "brave" and self.daily_search_limit > 30:
            raise ValidationError({"daily_search_limit": "Brave is capped at 30 attempts/day."})
        if self.provider in {"arbeitnow", "himalayas"} and not self.catalogue_mode:
            raise ValidationError({"catalogue_mode": "Free job feeds require catalogue mode."})
        for field, maximum, length in (("queries", 200, 400), ("technology_roles", 100, 80)):
            values = getattr(self, field)
            if field == "queries" and self.provider != "brave":
                continue
            if (
                not isinstance(values, list)
                or not 1 <= len(values) <= maximum
                or any(
                    not isinstance(value, str) or not value.strip() or len(value) > length
                    for value in values
                )
            ):
                raise ValidationError({field: "Provide a bounded nonempty list of strings."})

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(daily_search_limit__range=(1, 500))
                    & (~models.Q(provider="brave") | models.Q(daily_search_limit__lte=30))
                ),
                name="employer_search_limit",
            ),
            models.CheckConstraint(
                condition=models.Q(daily_candidate_limit__range=(1, 100)),
                name="employer_candidate_limit",
            ),
        ]

    def __str__(self):
        return self.name


class EmployerDiscoveryRun(models.Model):
    campaign = models.ForeignKey(EmployerDiscoveryCampaign, on_delete=models.PROTECT)
    status = models.CharField(
        max_length=12,
        default="active",
        choices=[("active", "Active"), ("complete", "Complete"), ("failed", "Failed")],
    )
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    next_attempt_at = models.DateTimeField(default=timezone.now, db_index=True)
    next_dispatch_at = models.DateTimeField(default=timezone.now)
    lease_token = models.UUIDField(null=True, editable=False)
    lease_until = models.DateTimeField(null=True, editable=False)
    pending_leads = models.JSONField(default=list, editable=False)
    retries = models.PositiveSmallIntegerField(default=0)
    searched = models.PositiveIntegerField(default=0)
    inspected = models.PositiveIntegerField(default=0)
    confirmed = models.PositiveIntegerField(default=0)
    companies_created = models.PositiveIntegerField(default=0)
    companies_existing = models.PositiveIntegerField(default=0)
    unresolved = models.PositiveIntegerField(default=0)
    duplicate = models.PositiveIntegerField(default=0)
    rejected = models.PositiveIntegerField(default=0)
    failed = models.PositiveIntegerField(default=0)
    diagnostic = models.CharField(max_length=80, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["campaign"],
                condition=models.Q(status="active"),
                name="one_active_employer_discovery",
            )
        ]


class EmployerDiscoveryBudget(models.Model):
    campaign = models.ForeignKey(EmployerDiscoveryCampaign, on_delete=models.PROTECT)
    day = models.DateField()
    searches = models.PositiveIntegerField(default=0)
    candidates = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["campaign", "day"], name="employer_campaign_daily_budget"
            )
        ]


class EmployerDiscoveryLead(models.Model):
    provenance = models.ForeignKey(
        "CompanyProvenance",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="observations",
    )
    reported_title = models.CharField(max_length=300, blank=True)
    reported_location = models.CharField(max_length=500, blank=True)
    reported_application_url = models.URLField(max_length=2048, blank=True)
    reported_expiry = models.DateTimeField(null=True, blank=True)
    reported_at = models.DateTimeField(null=True, blank=True)
    hiring_signal = models.BooleanField(default=False)
    campaign = models.ForeignKey(EmployerDiscoveryCampaign, on_delete=models.PROTECT)
    run = models.ForeignKey(EmployerDiscoveryRun, on_delete=models.PROTECT)
    key = models.CharField(max_length=64, editable=False)
    candidate_url = models.URLField(max_length=2048)
    search_query = models.CharField(max_length=400)
    search_offset = models.PositiveSmallIntegerField(default=0)
    name = models.CharField(max_length=250, blank=True)
    website = models.URLField(max_length=2048, blank=True)
    career_url = models.URLField(max_length=2048, blank=True)
    vacancy_title = models.CharField(max_length=300, blank=True)
    vacancy_location = models.CharField(max_length=500, blank=True)
    vacancy_url = models.URLField(max_length=2048, blank=True)
    relationship_url = models.URLField(max_length=2048, blank=True)
    observed_at = models.DateTimeField(null=True, blank=True)
    evidence_kind = models.CharField(max_length=20, blank=True)
    valid_until = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=12,
        default="unresolved",
        choices=[
            ("unresolved", "Unresolved"),
            ("confirmed", "Confirmed"),
            ("rejected", "Rejected"),
        ],
    )
    diagnostic = models.CharField(max_length=80, blank=True)
    company = models.ForeignKey(Company, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["campaign", "key"], name="employer_campaign_lead")
        ]

    def __str__(self):
        return self.name or self.candidate_url


class CompanyProvenance(models.Model):
    company = models.ForeignKey(
        Company, on_delete=models.CASCADE, related_name="discovery_provenance"
    )
    campaign = models.ForeignKey(EmployerDiscoveryCampaign, on_delete=models.PROTECT)
    provider = models.CharField(max_length=12, db_index=True)
    external_identity = models.CharField(max_length=512)
    source_url = models.URLField(max_length=2048)
    reported_name = models.CharField(max_length=250)
    reported_website = models.URLField(max_length=2048, blank=True)
    reported_domain = models.CharField(max_length=253, blank=True, db_index=True)
    regional_status = models.CharField(
        max_length=12,
        default="unknown",
        choices=[
            ("target", "Target region"),
            ("unknown", "Region unresolved"),
        ],
        db_index=True,
    )
    regional_evidence = models.CharField(max_length=500, blank=True)
    possible_duplicate = models.BooleanField(default=False, db_index=True)
    first_observed_at = models.DateTimeField(default=timezone.now)
    last_observed_at = models.DateTimeField(default=timezone.now)
    hiring_observed_at = models.DateTimeField(null=True, blank=True)
    priority = models.PositiveSmallIntegerField(default=0)
    source_lead = models.ForeignKey(
        EmployerDiscoveryLead,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="inspection_sources",
    )
    last_inspected_at = models.DateTimeField(null=True, blank=True)
    next_check_at = models.DateTimeField(default=timezone.now, db_index=True)
    next_dispatch_at = models.DateTimeField(default=timezone.now)
    lease_token = models.UUIDField(null=True, blank=True, editable=False)
    lease_until = models.DateTimeField(null=True, blank=True, editable=False)
    retries = models.PositiveSmallIntegerField(default=0)
    diagnostic = models.CharField(max_length=80, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "external_identity"], name="company_provider_identity"
            )
        ]

    def __str__(self):
        return f"{self.reported_name} ({self.provider})"
