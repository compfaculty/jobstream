from django.core.exceptions import ValidationError
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from .models import Application, BatchItem, Job
from .services.search import search_jobs
from .services.submissions import approve


class JobSerializer(serializers.ModelSerializer):
    class Meta:
        model = Job
        fields = (
            "id",
            "title",
            "company",
            "company_profile",
            "location",
            "description",
            "normalized",
            "active",
            "lifecycle",
            "status_override",
            "status_reason",
            "verification",
            "verified_at",
            "provider_key",
            "canonical_url",
            "apply_url",
            "discovered_at",
        )


class JobViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = JobSerializer

    @extend_schema(
        parameters=[
            OpenApiParameter("q", str),
            OpenApiParameter("location", str),
            OpenApiParameter("company", int, description="Exact company profile ID"),
        ]
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    def get_queryset(self):
        return search_jobs(
            self.request.query_params.get("q", ""),
            self.request.query_params.get("location", ""),
            self.request.query_params.get("company", ""),
        )


class ApplicationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Application
        fields = (
            "id",
            "job_id",
            "channel",
            "portal_access",
            "portal_url",
            "portal_answers",
            "portal_receipt_url",
            "recipient",
            "subject",
            "body",
            "status",
            "tracking_status",
            "notes",
            "provider_id",
            "error_code",
            "updated_at",
        )
        read_only_fields = fields


class ApplicationViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = ApplicationSerializer
    queryset = Application.objects.none()

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Application.objects.none()
        return Application.objects.filter(owner=self.request.user).order_by("-updated_at")

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        application = self.get_object()
        try:
            approve(request.user, application.pk)
        except ValidationError as exc:
            return Response({"errors": exc.messages}, status=400)
        application.refresh_from_db()
        return Response(self.get_serializer(application).data)


class BatchRequestSerializer(serializers.Serializer):
    document = serializers.IntegerField(min_value=1)
    jobs = serializers.ListField(
        child=serializers.IntegerField(min_value=1), min_length=1, max_length=100
    )
    request_key = serializers.UUIDField()
    profile = serializers.IntegerField(min_value=1, required=False)
    use_ai = serializers.BooleanField(default=False)
    portal_answers = serializers.JSONField(required=False)


class BatchSerializer(serializers.ModelSerializer):
    class Meta:
        from .models import ApplicationBatch

        model = ApplicationBatch
        fields = (
            "id",
            "document",
            "profile",
            "use_ai",
            "request_key",
            "cancelled_at",
            "created_at",
        )
        read_only_fields = fields


class BatchSelectionSerializer(serializers.Serializer):
    items = serializers.ListField(
        child=serializers.IntegerField(min_value=1), min_length=1, max_length=100
    )


class BatchItemSerializer(serializers.ModelSerializer):
    delivery_status = serializers.CharField(
        source="application.status", read_only=True, default=None, allow_null=True
    )

    class Meta:
        model = BatchItem
        fields = (
            "id",
            "job_id",
            "application_id",
            "state",
            "error_code",
            "next_attempt_at",
            "attempts",
            "delivery_status",
        )
        read_only_fields = fields


class BatchViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = BatchSerializer

    def get_queryset(self):
        from .models import ApplicationBatch

        if getattr(self, "swagger_fake_view", False):
            return ApplicationBatch.objects.none()
        return ApplicationBatch.objects.filter(owner=self.request.user).order_by("-created_at")

    @extend_schema(request=BatchRequestSerializer, responses={201: BatchSerializer})
    def create(self, request, *args, **kwargs):
        from .services.batches import create

        values = BatchRequestSerializer(data=request.data)
        values.is_valid(raise_exception=True)
        data = values.validated_data
        try:
            batch = create(
                request.user,
                data["document"],
                data["jobs"],
                data["request_key"],
                profile_id=data.get("profile"),
                use_ai=data["use_ai"],
                portal_answers=data.get("portal_answers"),
            )
        except ValidationError as exc:
            return Response({"errors": exc.messages}, status=400)
        return Response(self.get_serializer(batch).data, status=201)

    @extend_schema(responses=BatchItemSerializer(many=True))
    @action(detail=True, methods=["get"])
    def items(self, request, pk=None):
        batch = self.get_object()
        return Response(
            BatchItemSerializer(
                batch.items.select_related("application").order_by("pk"), many=True
            ).data
        )

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        from .services.batches import cancel

        batch = self.get_object()
        cancel(request.user, batch.pk)
        return Response({"cancelled": True})

    @extend_schema(request=BatchSelectionSerializer)
    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        from .services.batches import approve_items

        batch = self.get_object()
        selection_form = BatchSelectionSerializer(data=request.data)
        selection_form.is_valid(raise_exception=True)
        selection = selection_form.validated_data["items"]
        try:
            approve_items(request.user, batch.pk, selection)
        except ValidationError as exc:
            return Response({"errors": exc.messages}, status=400)
        return Response({"approved": selection})
