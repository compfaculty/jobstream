from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView
from rest_framework.routers import DefaultRouter
from two_factor.urls import urlpatterns as two_factor_urls

from jobs import evaluation_views, operations_views, views
from jobs.api import ApplicationViewSet, BatchViewSet, JobViewSet

router = DefaultRouter()
router.register("batches", BatchViewSet, basename="api-batch")
router.register("jobs", JobViewSet, basename="api-job")
router.register("applications", ApplicationViewSet, basename="api-application")
urlpatterns = [
    path("operations/", operations_views.index, name="operations"),
    path(
        "operations/executions/<uuid:pk>/", operations_views.execution, name="operation-execution"
    ),
    path("jobs/<int:pk>/evaluate/", evaluation_views.start, name="evaluation-start"),
    path("jobs/<int:pk>/evaluations/", evaluation_views.panel, name="evaluation-panel"),
    path(
        "evaluations/<uuid:pk>/<str:operation>/", evaluation_views.action, name="evaluation-action"
    ),
    path(
        "internal/agent/v1/context/<uuid:pk>/<uuid:attempt_id>/",
        evaluation_views.context,
        name="evaluation-context",
    ),
    path("favicon.ico", views.favicon, name="favicon"),
    path("", views.job_list, name="jobs"),
    path("companies/<int:pk>/", views.company_detail, name="company"),
    path("companies/<int:pk>/logo/", views.company_logo, name="company-logo"),
    path("jobs/<int:pk>/", views.job_detail, name="job"),
    path("jobs/<int:pk>/save/", views.toggle_saved, name="save-job"),
    path("jobs/<int:pk>/apply/", views.application_create, name="application-create"),
    path("batches/", views.batch_list, name="batches"),
    path("batches/new/", views.batch_create, name="batch-create"),
    path("batches/<int:pk>/", views.batch_detail, name="batch"),
    path("activity/", views.activity, name="activity"),
    path("applications/", views.applications, name="applications"),
    path("applications/<int:pk>/", views.application_detail, name="application"),
    path("applications/<int:pk>/edit/", views.application_edit, name="application-edit"),
    path("applications/<int:pk>/approve/", views.application_approve, name="application-approve"),
    path("applications/<int:pk>/tracking/", views.tracking, name="tracking"),
    path("preferences/", views.application_preferences, name="application-preferences"),
    path("documents/", views.documents, name="documents"),
    path("documents/<int:pk>/profile/", views.resume_profile, name="resume-profile"),
    path("documents/<int:pk>/", views.document_download, name="document-download"),
    path("gmail/", views.gmail_settings, name="gmail"),
    path("gmail/connect/", views.gmail_connect, name="gmail-connect"),
    path("gmail/callback/", views.gmail_callback, name="gmail-callback"),
    path("gmail/disconnect/", views.gmail_disconnect, name="gmail-disconnect"),
    path("invite/<str:token>/", views.register, name="register"),
    path("logout/", views.signout, name="logout"),
    path("", include(two_factor_urls)),
    path("admin/", admin.site.urls),
    path("api/v1/", include(router.urls)),
    path("api/v1/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("health/", views.health, name="health"),
]
