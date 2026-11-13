import secrets
import time

from botocore.exceptions import ClientError
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.db.models.deletion import ProtectedError
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST
from google_auth_oauthlib.flow import Flow

from .forms import ApplicationForm, RegistrationForm, TrackingForm, UploadForm
from .models import Application, AuditEvent, Company, GmailConnection, Invitation, Job, SavedJob
from .services import recommendation_runs, submissions
from .services.authorization import owned_application, owned_document
from .services.categories import CATEGORIES, category_filter, resolve_category
from .services.documents import base_documents, current_base, pdf_filename, upload_document
from .services.gmail import encrypt_credentials, gmail_client
from .services.recommendations import (
    evaluated_jobs,
    evaluations_pending,
    recommended_jobs,
    sources_for,
)
from .services.search import search_jobs


def register(request, token):
    invitation = get_object_or_404(
        Invitation, token=token, used_at__isnull=True, expires_at__gt=timezone.now()
    )
    form = RegistrationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            invitation = Invitation.objects.select_for_update().get(pk=invitation.pk)
            if invitation.used_at or invitation.expires_at <= timezone.now():
                raise Http404
            user = form.save(commit=False)
            user.email = invitation.email
            user.save()
            invitation.used_at = timezone.now()
            invitation.save(update_fields=["used_at"])
            AuditEvent.objects.create(actor=user, event="account.registered")
        login(request, user, backend="django.contrib.auth.backends.ModelBackend")
        return redirect("jobs")
    return render(
        request,
        "jobs/form.html",
        {"form": form, "title": "Accept invitation", "button": "Create account"},
    )


@login_required
def job_list(request):
    query = request.GET.get("q", "")[:200]
    location = request.GET.get("location", "")[:100]
    company = request.GET.get("company", "")[:40]
    category = resolve_category(request.GET.get("category", "all"))
    mode = "recommended" if request.GET.get("mode") == "recommended" else "all"
    recommendation_context = {}
    jobs = search_jobs(query, location, company, include_inactive=mode == "all").filter(
        category_filter(category)
    )
    recommendation_context = sources_for(request.user, request.GET.get("cv", "")[:80])
    source = recommendation_context["recommendation_source"]
    jobs = (
        recommended_jobs(jobs, request.user, source)
        if mode == "recommended"
        else evaluated_jobs(jobs, request.user, source)
    )
    recommendation_context.update(recommendation_runs.latest(request.user, source))
    recommendation_context["recommendation_poll"] = recommendation_context.get(
        "recommendation_poll", False
    ) or evaluations_pending(request.user, source)
    if request.GET.get("saved") == "1":
        jobs = jobs.filter(savedjob__owner=request.user)
    page = Paginator(jobs, 25).get_page(request.GET.get("page"))
    template = (
        "jobs/list_results.html"
        if request.headers.get("HX-Request") == "true"
        else "jobs/list.html"
    )
    companies = Company.objects.filter(Exists(Job.objects.filter(company_profile=OuterRef("pk"))))
    return render(
        request,
        template,
        {
            **recommendation_context,
            "job_mode": mode,
            "page": page,
            "q": query,
            "location": location,
            "companies": companies,
            "selected_company": company,
            "categories": CATEGORIES,
            "selected_category": category,
        },
    )


@login_required
def job_detail(request, pk):
    job = get_object_or_404(
        Job.objects.select_related("company_profile").prefetch_related("origins__source"), pk=pk
    )
    executions = list(
        request.user.agentexecution_set.filter(job=job)
        .select_related("document", "profile")
        .order_by("-created_at")[:10]
    )
    return render(
        request,
        "jobs/detail.html",
        {
            "job": job,
            "evaluation_enabled": settings.AGENT_BACKEND == "infinity",
            "evaluation_profiles": request.user.resumeprofile_set.filter(
                document=current_base(request.user)
            ).order_by("-pk")[:1],
            "evaluation_documents": base_documents(request.user),
            "evaluations": executions,
            "evaluation_poll": settings.AGENT_BACKEND == "infinity"
            and any(item.status in {"queued", "running"} for item in executions),
            "saved": SavedJob.objects.filter(owner=request.user, job=job).exists(),
            "application": Application.objects.filter(owner=request.user, job=job).first(),
        },
    )


@login_required
def company_detail(request, pk):
    company = get_object_or_404(Company.objects.all(), pk=pk)
    page = Paginator(search_jobs(company=str(company.pk)), 25).get_page(request.GET.get("page"))
    return render(request, "jobs/company.html", {"company": company, "page": page})


@login_required
def company_logo(request, pk):
    company = get_object_or_404(Company, pk=pk)
    if not company.logo:
        raise Http404
    try:
        response = FileResponse(company.logo.open("rb"), content_type="image/png")
    except (FileNotFoundError, ClientError) as exc:
        if isinstance(exc, ClientError) and exc.response.get("Error", {}).get("Code") not in {
            "404",
            "NoSuchKey",
            "NotFound",
        }:
            raise
        # A stale legacy reference should not render a broken image in the list.
        response = HttpResponse(
            '<svg xmlns="http://www.w3.org/2000/svg" width="48" height="48" '
            'viewBox="0 0 48 48"><rect width="48" height="48" rx="8" fill="#303842"/>'
            '<text x="24" y="34" text-anchor="middle" font-family="sans-serif" '
            'font-size="30" font-weight="bold" fill="#aab5bf">?</text></svg>',
            content_type="image/svg+xml",
        )
        response["Cache-Control"] = "no-store"
        return response
    response["Content-Disposition"] = 'inline; filename="company-logo.png"'
    return response


@login_required
@require_POST
def toggle_saved(request, pk):
    job = get_object_or_404(Job, pk=pk)
    saved, created = SavedJob.objects.get_or_create(owner=request.user, job=job)
    if not created:
        saved.delete()
    AuditEvent.objects.create(
        actor=request.user,
        event="job.saved" if created else "job.unsaved",
        details={"job_id": job.pk},
    )
    return redirect("job", pk=pk)


@login_required
def application_create(request, pk):
    job = get_object_or_404(Job.objects.select_related("company_profile"), pk=pk)
    existing = Application.objects.filter(owner=request.user, job=job).first()
    if existing:
        return redirect("application", pk=existing.pk)
    from .services.secret_telaviv import secret_url

    draft = Application(owner=request.user, job=job)
    if not job.preferred_application_email:
        draft.channel = "portal"
        draft.portal_url = job.apply_url or job.canonical_url
    try:
        draft.portal_url = secret_url(job.apply_url or job.canonical_url)
        if not job.preferred_application_email:
            draft.channel = "portal"
    except ValueError:
        pass
    from .services.cato_greenhouse import job_url

    if not job.preferred_application_email:
        try:
            draft.portal_url = job_url(job)
            draft.channel = "portal"
        except ValueError:
            pass
    from .services.camunda_ashby import job_url as camunda_url

    if not job.preferred_application_email:
        try:
            draft.portal_url = camunda_url(job)
            draft.channel = "portal"
        except ValueError:
            pass
    from .services.gitlab_greenhouse import job_url as gitlab_url

    if not job.preferred_application_email:
        try:
            draft.portal_url = gitlab_url(job)
            draft.channel = "portal"
        except ValueError:
            pass
    from .services.comeet import manual_url

    if not job.preferred_application_email and (handoff := manual_url(job)):
        draft.portal_url = handoff
        draft.channel = "portal"
    form = ApplicationForm(
        request.POST or None,
        owner=request.user,
        instance=draft,
        initial={
            "recipient": job.preferred_application_email,
            "subject": f"Application: {job.title}"[:250],
            "document": getattr(current_base(request.user), "pk", None),
        },
    )
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            # Serialize same-user creation to avoid duplicate double-click races.
            from django.contrib.auth import get_user_model

            get_user_model().objects.select_for_update().get(pk=request.user.pk)
            application = Application.objects.filter(owner=request.user, job=job).first()
            if application is None:
                application = form.save(commit=False)
                application.owner = request.user
                application.job = job
                application.save()
                AuditEvent.objects.create(
                    actor=request.user, application=application, event="application.created"
                )
        from .services.cv_workspace import generate

        try:
            generate(request.user, application.pk)
        except ValidationError as exc:
            messages.warning(request, str(exc))
        return redirect("application", pk=application.pk)
    return render(
        request,
        "jobs/form.html",
        {"form": form, "title": "Prepare application", "button": "Prepare CV and application"},
    )


@login_required
def application_detail(request, pk):
    application = owned_application(request.user, pk)
    from .services import gitlab_greenhouse as gitlab
    from .services.camunda_ashby import PortalRejected, inspect_schema
    from .services.camunda_ashby import supported as camunda_supported
    from .services.cato_greenhouse import supported as cato_supported
    from .services.comeet import manual_url
    from .services.secret_telaviv import supported

    has_application_cv = bool(
        application.document_id
        and application.document.prepared_resumes.filter(owner=request.user).exists()
    )
    gitlab_review = []
    if gitlab.supported(application):
        try:
            schema = gitlab.inspect_schema(
                application.portal_answers.get("_schema", {}), gitlab.posting_id(application.job)
            )
            for q in schema.entries:
                f = q["fields"][0]
                value = application.portal_answers.get(f["name"], "")
                if f["type"] == "multi_value_single_select":
                    value = next((v["label"] for v in f["values"] if v["value"] == value), "")
                gitlab_review.append((q["label"], value))
        except gitlab.PortalRejected:
            pass
    camunda_review = []
    if camunda_supported(application):
        try:
            schema = inspect_schema(
                application.portal_answers.get("_schema", {}),
                application.job.provider_key.rsplit(":", 1)[-1],
            )
            for entry in schema.entries:
                field = entry["field"]
                if field["type"] != "File":
                    camunda_review.append(
                        (
                            field["title"] or "Application data consent",
                            application.portal_answers.get(field["path"], "Unanswered"),
                        )
                    )
        except PortalRejected:
            pass
    return render(
        request,
        "jobs/application.html",
        {
            "application": application,
            "tracking_form": TrackingForm(instance=application),
            "base_cv": current_base(request.user),
            "has_application_cv": has_application_cv,
            "activity": AuditEvent.objects.filter(application=application).order_by(
                "-created_at", "-pk"
            ),
            "can_edit": application.status in {"draft", "approved", "failed"},
            "akeyless_manual_url": manual_url(application.job)
            if application.channel == "portal"
            else "",
            "secret_portal": supported(application),
            "cato_portal": cato_supported(application),
            "camunda_portal": camunda_supported(application),
            "camunda_review": camunda_review,
            "gitlab_portal": gitlab.supported(application),
            "assisted_supported": (
                cato_supported(application)
                or camunda_supported(application)
                or gitlab.supported(application)
            ),
            "gitlab_review": gitlab_review,
            "can_approve": (
                application.channel == "email"
                or supported(application)
                or cato_supported(application)
                or camunda_supported(application)
                or gitlab.supported(application)
            )
            and application.status in {"draft", "failed"}
            and has_application_cv,
        },
    )


@login_required
@require_POST
def application_cv(request, pk):
    application = owned_application(request.user, pk)
    from .services.cv_workspace import generate

    if application.status not in {"draft", "failed", "approved"}:
        return HttpResponse("The submitted CV cannot be changed.", status=400)
    # Older clients can still post a profile ID, but it can never select another owner's data.
    if (
        request.POST.get("profile")
        and not request.user.resumeprofile_set.filter(pk=request.POST["profile"]).exists()
    ):
        return HttpResponse("Select your base CV.", status=400)
    try:
        generate(request.user, application.pk)
    except (ValidationError, submissions.InvalidTransition) as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Application CV generated. Review or edit it before sending.")
    return redirect("application", pk=application.pk)


@login_required
def application_edit(request, pk):
    application = owned_application(request.user, pk)
    form = ApplicationForm(request.POST or None, owner=request.user, instance=application)
    if request.method == "POST" and form.is_valid():
        try:
            submissions.edit(request.user, pk, form.cleaned_data)
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            return redirect("application", pk=pk)
    return render(
        request,
        "jobs/form.html",
        {
            "form": form,
            "title": "Edit application — approval will be cleared",
            "button": "Save draft",
        },
    )


@login_required
@require_POST
def application_approve(request, pk):
    owned_application(request.user, pk)
    try:
        application = submissions.approve(
            request.user, pk, assisted=request.POST.get("assisted") == "1"
        )
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    else:
        messages.success(
            request,
            "Application approved, awaiting assisted submission. It has not been sent."
            if application.assisted_delivery
            else "Approved. Your application is queued for submission.",
        )
    return redirect("application", pk=pk)


@login_required
@require_POST
def tracking(request, pk):
    application = owned_application(request.user, pk)
    form = TrackingForm(request.POST, instance=application)
    if form.is_valid():
        with transaction.atomic():
            previous = Application.objects.select_for_update().get(pk=pk, owner=request.user)
            Application.objects.filter(pk=pk, owner=request.user).update(**form.cleaned_data)
            AuditEvent.objects.create(
                actor=request.user,
                application=application,
                event=f"tracking.{form.cleaned_data['tracking_status']}",
                details={
                    "from": previous.tracking_status,
                    "to": form.cleaned_data["tracking_status"],
                    "notes_changed": previous.notes != form.cleaned_data["notes"],
                },
            )
    return redirect("application", pk=pk)


@login_required
def applications(request):
    page = Paginator(
        Application.objects.filter(owner=request.user)
        .select_related("job")
        .order_by("-updated_at"),
        25,
    ).get_page(request.GET.get("page"))
    return render(request, "jobs/applications.html", {"page": page})


@login_required
def documents(request):
    form = UploadForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        try:
            upload_document(request.user, form.cleaned_data["file"])
        except ValidationError as exc:
            form.add_error("file", exc)
        else:
            return redirect("documents")
    return render(
        request,
        "jobs/documents.html",
        {"form": form, "base": current_base(request.user)},
    )


@login_required
@require_http_methods(["GET", "POST"])
def document_delete(request, pk):
    document = owned_document(request.user, pk)
    if request.method == "POST":
        try:
            with transaction.atomic():
                document.delete()
                transaction.on_commit(lambda: document.file.delete(save=False), robust=True)
        except ProtectedError:
            messages.error(
                request,
                "This resume is used by an application, CV profile, batch, or evaluation "
                "and cannot be deleted. Existing records keep their original resume.",
            )
        else:
            messages.success(request, "Resume deleted.")
        return redirect("documents")
    return render(request, "jobs/document_delete.html", {"document": document})


@login_required
def document_download(request, pk):
    document = owned_document(request.user, pk)
    return FileResponse(
        document.file.open("rb"),
        as_attachment=True,
        filename=pdf_filename(document.name),
        content_type="application/pdf",
    )


def oauth_flow(state=None):
    return Flow.from_client_config(
        {
            "web": {
                "client_id": settings.GOOGLE_CLIENT_ID,
                "client_secret": settings.GOOGLE_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        },
        scopes=settings.GOOGLE_SCOPES,
        state=state,
        redirect_uri=settings.GOOGLE_REDIRECT_URI,
        autogenerate_code_verifier=True,
    )


@login_required
def gmail_settings(request):
    return render(
        request,
        "jobs/gmail.html",
        {
            "connection": GmailConnection.objects.filter(owner=request.user).first(),
            "configured": bool(settings.GOOGLE_CLIENT_ID and settings.TOKEN_ENCRYPTION_KEY),
        },
    )


@login_required
@require_POST
def gmail_connect(request):
    if not (
        settings.GOOGLE_CLIENT_ID
        and settings.GOOGLE_CLIENT_SECRET
        and settings.TOKEN_ENCRYPTION_KEY
    ):
        messages.error(request, "Gmail is not configured by the operator yet.")
        return redirect("gmail")
    flow = oauth_flow()
    url, state = flow.authorization_url(access_type="offline", prompt="consent")
    request.session["gmail_oauth"] = {
        "state": state,
        "verifier": flow.code_verifier,
        "created": time.time(),
        "user": request.user.pk,
    }
    return redirect(url)


@login_required
def gmail_callback(request):
    pending = request.session.pop("gmail_oauth", {})
    if (
        pending.get("user") != request.user.pk
        or time.time() - pending.get("created", 0) > 600
        or not secrets.compare_digest(pending.get("state", ""), request.GET.get("state", ""))
        or not pending.get("state")
        or not request.GET.get("code")
    ):
        return HttpResponse("Invalid or expired OAuth callback.", status=400)
    try:
        flow = oauth_flow(pending["state"])
        flow.code_verifier = pending["verifier"]
        flow.fetch_token(code=request.GET["code"])
        credentials = flow.credentials
        if not credentials.refresh_token or not set(settings.GOOGLE_SCOPES).issubset(
            set(credentials.granted_scopes or credentials.scopes)
        ):
            raise ValueError("Missing permissions")
        connection = GmailConnection(
            owner=request.user, encrypted_credentials=encrypt_credentials(credentials)
        )
        profile = gmail_client(connection).users().getProfile(userId="me").execute(num_retries=0)
        with transaction.atomic():
            GmailConnection.objects.update_or_create(
                owner=request.user,
                defaults={
                    "email": profile["emailAddress"],
                    "encrypted_credentials": connection.encrypted_credentials,
                    "revoked": False,
                },
            )
            AuditEvent.objects.create(actor=request.user, event="gmail.connected")
        messages.success(request, "Gmail connected.")
    except Exception:
        messages.error(
            request,
            "Gmail connection failed. Please reconnect and grant the requested permissions.",
        )
    return redirect("gmail")


@login_required
@require_POST
def gmail_disconnect(request):
    # Local disconnect is immediate. Google-side revocation can also be done in account settings.
    GmailConnection.objects.filter(owner=request.user).update(
        encrypted_credentials="", revoked=True
    )
    AuditEvent.objects.create(actor=request.user, event="gmail.disconnected")
    return redirect("gmail")


@require_POST
def signout(request):
    logout(request)
    return redirect("two_factor:login")


def health(request):
    return JsonResponse({"status": "ok"})


def favicon(request):
    return HttpResponse(status=204)


@login_required
def batch_create(request):
    import uuid

    from .forms import SecretApplicantForm
    from .services.batches import create

    ids = request.POST.getlist("jobs")
    selected_ids = [value for value in ids if value.isdecimal()]
    has_secret = (
        Job.objects.filter(pk__in=selected_ids)
        .filter(
            Q(canonical_url__startswith="https://jobs.secrettelaviv.com/job/")
            | Q(apply_url__startswith="https://jobs.secrettelaviv.com/job/")
        )
        .exists()
    )
    supplied_applicant = has_secret and any(
        request.POST.get(f"secret-{key}") for key in ("first_name", "last_name", "email", "phone")
    )
    initial_answers = {}
    if has_secret:
        from .services.saved_answers import prefill_contact

        try:
            initial_answers = prefill_contact(request.user.pk)
        except ValidationError:
            messages.warning(request, "Saved contact answers are unavailable; enter them below.")
    applicant_form = SecretApplicantForm(
        request.POST if supplied_applicant or request.POST.get("document") else None,
        initial=initial_answers,
        prefix="secret",
    )
    for field in applicant_form.fields.values():
        field.required = False
    if request.method == "POST" and request.POST.get("document"):
        try:
            if supplied_applicant and not applicant_form.is_valid():
                raise ValidationError("Complete the Secret Tel Aviv applicant details and terms.")
            base = current_base(request.user)
            profile = (
                base.profiles.filter(owner=request.user).order_by("-pk").first() if base else None
            )
            if not profile:
                raise ValidationError("Edit and save your base CV before preparing applications.")
            batch = create(
                request.user,
                base.pk,
                [int(value) for value in ids],
                uuid.UUID(request.POST["request_key"]),
                profile_id=profile.pk,
                use_ai=request.POST.get("use_ai") == "1",
                check_fit=request.POST.get("check_fit") == "1",
                portal_answers=applicant_form.answers() if supplied_applicant else {},
            )
        except (ValidationError, ValueError, KeyError) as exc:
            messages.error(request, str(exc))
        else:
            return redirect("batch", pk=batch.pk)
    return render(
        request,
        "jobs/batch_create.html",
        {
            "jobs": Job.objects.filter(pk__in=[value for value in ids if value.isdecimal()]),
            "documents": base_documents(request.user),
            "request_key": uuid.uuid4(),
            "applicant_form": applicant_form,
            "profiles": request.user.resumeprofile_set.filter(
                document=current_base(request.user)
            ).order_by("-pk")[:1],
        },
    )


@login_required
def batch_detail(request, pk):
    from .models import ApplicationBatch
    from .services.batches import approve_items, cancel, recheck_fit

    batch = get_object_or_404(ApplicationBatch, pk=pk, owner=request.user)
    if request.method == "POST":
        try:
            if request.POST.get("action") == "recheck_fit":
                recheck_fit(request.user, batch.pk)
            elif request.POST.get("action") == "cancel":
                cancel(request.user, batch.pk)
            else:
                approve_items(
                    request.user, batch.pk, [int(value) for value in request.POST.getlist("items")]
                )
        except (ValidationError, ValueError) as exc:
            messages.error(request, str(exc))
        return redirect("batch", pk=pk)
    items = list(batch.items.select_related("job", "application").order_by("pk"))
    polling = any(
        item.state in {"queued", "preparing"}
        or (
            item.application_id and item.application.status in {"approved", "submitting", "unknown"}
        )
        for item in items
    )
    return render(
        request,
        "jobs/batch_items.html"
        if request.headers.get("HX-Request") == "true"
        else "jobs/batch.html",
        {"batch": batch, "items": items, "polling": polling},
    )


def cv_editor(request, *, document_id=None, application_id=None):
    from .forms import CVEditorForm
    from .services.cv_workspace import editor_initial, save_editor

    application = owned_application(request.user, application_id) if application_id else None
    document = application.document if application else owned_document(request.user, document_id)
    if application:
        if application.status not in {"draft", "failed", "approved"} or not document:
            return HttpResponse("The submitted CV is read-only.", status=409)
    elif not document or document.pk != getattr(current_base(request.user), "pk", None):
        raise Http404
    try:
        initial = editor_initial(request.user, document)
    except ValidationError as exc:
        messages.error(request, str(exc))
        return redirect("documents")
    form = CVEditorForm(request.POST if request.method == "POST" else None, initial=initial)
    if request.method == "POST" and form.is_valid():
        try:
            save_editor(request.user, document, form.cleaned_data, application=application)
        except (ValidationError, submissions.InvalidTransition) as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(
                request,
                "CV saved. Review the updated PDF before sending."
                if application
                else "Base CV saved.",
            )
            return (
                redirect("application", pk=application.pk) if application else redirect("documents")
            )
    return render(request, "jobs/cv_editor.html", {"form": form, "application": application})


@login_required
@require_http_methods(["GET", "POST"])
def base_cv_edit(request):
    base = current_base(request.user)
    if not base:
        return redirect("documents")
    return cv_editor(request, document_id=base.pk)


@login_required
@require_http_methods(["GET", "POST"])
def application_cv_edit(request, pk):
    return cv_editor(request, application_id=pk)


@login_required
def resume_profile(request, pk):
    from .forms import ResumeProfileForm
    from .services.profile_editor import (
        CONTACT_FIELDS,
        confirmed_sections,
        entry_sets,
        serialize_entries,
    )
    from .services.resumes import SECTIONS, confirm, extract

    document = owned_document(request.user, pk)
    latest = document.profiles.filter(owner=request.user).order_by("-pk").first()
    initial = {}
    extracted = ""
    try:
        extracted = extract(document)
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    prefilled = not latest and request.method == "GET" and bool(extracted)
    if prefilled:
        from .services.profile_prefill import from_text

        initial = from_text(extracted)
    if latest:
        if latest.details:
            initial = latest.details.get("basics", {}).copy()
        else:
            initial = {"contact": latest.contact}
            for section in SECTIONS:
                initial[section.lower()] = "\n\n".join(
                    fact["text"] for fact in latest.facts if fact["section"] == section
                )
        initial["name"] = latest.name
    data = request.POST if request.method == "POST" else None
    form = ResumeProfileForm(data, initial=initial, label_suffix="")
    if prefilled:
        for field in ("experience", "education", "projects"):
            form.fields[field].label = f"{field.title()} imported from your PDF"
    groups = entry_sets(data, latest.details if latest else None)
    if request.method == "POST":
        valid = form.is_valid()
        group_validity = [group["forms"].is_valid() for group in groups]
        if valid and all(group_validity):
            entries = serialize_entries(groups)
            values = form.cleaned_data
            contact = " | ".join(values[key] for key in CONTACT_FIELDS if values[key])
            if len(contact) > 500:
                form.add_error(None, "Combined contact details must be at most 500 characters.")
            else:
                try:
                    confirm(
                        request.user,
                        document,
                        values["name"],
                        contact,
                        confirmed_sections(values, entries),
                        details={
                            "basics": {
                                key: value for key, value in values.items() if key != "confirmed"
                            },
                            **entries,
                        },
                    )
                except ValidationError as exc:
                    form.add_error(None, exc)
                else:
                    messages.success(
                        request, "Profile saved. Existing batches retain their profile version."
                    )
                    return redirect("documents")
    return render(
        request,
        "jobs/profile.html",
        {
            "form": form,
            "document": document,
            "entry_groups": groups,
            "extracted": extracted,
            "prefilled": prefilled,
        },
    )


@login_required
def batch_list(request):
    from .models import ApplicationBatch

    page = Paginator(
        ApplicationBatch.objects.filter(owner=request.user)
        .select_related("document")
        .order_by("-created_at"),
        25,
    ).get_page(request.GET.get("page"))
    return render(request, "jobs/batches.html", {"page": page})


@login_required
def activity(request):
    events = (
        AuditEvent.objects.filter(
            Q(application__owner=request.user) | Q(actor=request.user, application__isnull=True)
        )
        .select_related("application__job")
        .order_by("-created_at", "-pk")
    )
    return render(
        request,
        "jobs/activity.html",
        {"page": Paginator(events, 50).get_page(request.GET.get("page"))},
    )


@login_required
def application_preferences(request):
    from .forms import ApplicationPreferencesForm, CommonAnswersForm
    from .models import ApplicationPreferences, SavedApplicationAnswers
    from .services.saved_answers import read_answers, save_answers

    preferences = ApplicationPreferences.objects.filter(owner=request.user).first()
    initial = {}
    if preferences:
        for name in ApplicationPreferencesForm.base_fields:
            value = getattr(preferences, name)
            initial[name] = (
                "\n".join(value) if name in {"preferred_locations", "preferred_titles"} else value
            )
    answer_initial = {}
    has_saved_answers = SavedApplicationAnswers.objects.filter(owner=request.user).exists()
    try:
        answers, reuse = read_answers(request.user.pk)
        answer_initial = {**answers, "reuse_enabled": reuse}
    except ValidationError as exc:
        messages.warning(request, "; ".join(exc.messages))
    action = request.POST.get("action", "save_preferences") if request.method == "POST" else None
    form = ApplicationPreferencesForm(
        request.POST if action == "save_preferences" else None, initial=initial
    )
    answer_form = CommonAnswersForm(
        request.POST if action == "save_answers" else None, initial=answer_initial, prefix="answers"
    )
    if action == "delete_answers":
        SavedApplicationAnswers.objects.filter(owner=request.user).delete()
        messages.success(request, "Saved application answers deleted.")
        return redirect("application-preferences")
    if action == "save_preferences" and form.is_valid():
        ApplicationPreferences.objects.update_or_create(
            owner=request.user, defaults=form.cleaned_data
        )
        messages.success(request, "Application preferences saved.")
        return redirect("application-preferences")
    if action == "save_answers" and answer_form.is_valid():
        data = dict(answer_form.cleaned_data)
        reuse = data.pop("reuse_enabled")
        try:
            save_answers(request.user.pk, data, reuse_enabled=reuse)
        except ValidationError as exc:
            answer_form.add_error(None, exc)
        else:
            messages.success(request, "Common application answers saved.")
            return redirect("application-preferences")
    return render(
        request,
        "jobs/preferences.html",
        {
            "form": form,
            "answer_form": answer_form,
            "has_saved_answers": has_saved_answers,
        },
    )
