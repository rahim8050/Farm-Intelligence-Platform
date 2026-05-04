from typing import Any

from rest_framework import status, viewsets
from rest_framework.request import Request
from rest_framework.response import Response

from activities.models import Activity
from activities.serializers import (
    ActivityCreateSerializer,
    ActivitySerializer,
)
from config.api.responses import success_response


class ActivityViewSet(viewsets.ModelViewSet):
    """Activity CRUD operations.

    Auth: IsAuthenticated
    """

    serializer_class = ActivitySerializer

    def get_queryset(self) -> Any:
        user = self.request.user
        return Activity.objects.filter(owner=user).select_related("farm")

    def list(
        self, request: Request, *args: object, **kwargs: object
    ) -> Response:
        queryset = self.filter_queryset(self.get_queryset())
        serializer = self.get_serializer(queryset, many=True)
        return success_response(list(serializer.data))

    def retrieve(
        self, request: Request, *args: object, **kwargs: object
    ) -> Response:
        instance = self.get_object()
        serializer = self.get_serializer(instance)
        return success_response(serializer.data)

    def create(
        self, request: Request, *args: object, **kwargs: object
    ) -> Response:
        serializer = ActivityCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        activity = serializer.save(owner=request.user)
        return success_response(
            ActivitySerializer(activity).data,
            status_code=status.HTTP_201_CREATED,
        )

    def partial_update(
        self, request: Request, *args: object, **kwargs: object
    ) -> Response:
        instance = self.get_object()
        serializer = ActivitySerializer(
            instance, data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        activity = serializer.save()
        return success_response(ActivitySerializer(activity).data)

    def destroy(
        self, request: Request, *args: object, **kwargs: object
    ) -> Response:
        instance = self.get_object()
        self.perform_destroy(instance)
        return Response(status=status.HTTP_204_NO_CONTENT)
