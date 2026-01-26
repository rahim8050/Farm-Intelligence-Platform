from __future__ import annotations

# ruff: noqa: S101
import secrets
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ValidationError
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory
from rest_framework.views import APIView

from farms.models import Farm
from farms.permissions import IsFarmOwner
from farms.serializers import FarmSerializer
from farms.views import FarmViewSet


@pytest.mark.django_db
def test_farm_str_includes_owner_id() -> None:
    password = secrets.token_urlsafe(12)
    user = get_user_model().objects.create_user(
        username="farmer",
        email="farmer@example.com",
        password=password,
    )
    farm = Farm.objects.create(owner=user, name="Demo Farm", slug="demo")
    assert str(farm) == f"Demo Farm ({user.id})"


@pytest.mark.django_db
def test_farm_clean_requires_complete_bbox() -> None:
    password = secrets.token_urlsafe(12)
    user = get_user_model().objects.create_user(
        username="bbox-user",
        email="bbox@example.com",
        password=password,
    )
    farm = Farm(
        owner=user,
        name="BBox",
        slug="bbox",
        bbox_south=Decimal("0.0"),
    )
    with pytest.raises(ValidationError, match="Bounding box must include"):
        farm.full_clean()


@pytest.mark.django_db
def test_farm_clean_rejects_invalid_bbox_order() -> None:
    password = secrets.token_urlsafe(12)
    user = get_user_model().objects.create_user(
        username="order-user",
        email="order@example.com",
        password=password,
    )
    farm = Farm(
        owner=user,
        name="Order Farm",
        slug="order",
        bbox_south=Decimal("0.0"),
        bbox_west=Decimal("37.0"),
        bbox_north=Decimal("1.0"),
        bbox_east=Decimal("36.0"),
    )
    with pytest.raises(ValidationError, match="bbox_west must be <"):
        farm.full_clean()


@pytest.mark.django_db
def test_farm_clean_rejects_south_north_order() -> None:
    password = secrets.token_urlsafe(12)
    user = get_user_model().objects.create_user(
        username="order-lat",
        email="order-lat@example.com",
        password=password,
    )
    farm = Farm(
        owner=user,
        name="Order Lat Farm",
        slug="order-lat",
        bbox_south=Decimal("1.0"),
        bbox_west=Decimal("36.0"),
        bbox_north=Decimal("0.5"),
        bbox_east=Decimal("37.0"),
    )
    with pytest.raises(ValidationError, match="bbox_south must be <"):
        farm.full_clean()


@pytest.mark.django_db
def test_farm_clean_requires_complete_centroid() -> None:
    password = secrets.token_urlsafe(12)
    user = get_user_model().objects.create_user(
        username="centroid-user",
        email="centroid@example.com",
        password=password,
    )
    farm = Farm(
        owner=user,
        name="Centroid Farm",
        slug="centroid",
        centroid_lat=Decimal("0.1"),
    )
    with pytest.raises(ValidationError, match="Centroid requires both"):
        farm.full_clean()


@pytest.mark.django_db
def test_farm_serializer_bbox_and_centroid_validation() -> None:
    serializer = FarmSerializer(
        data={"name": "Partial bbox", "bbox_south": 0.0}
    )
    assert not serializer.is_valid()
    assert (
        "Bounding box must include" in serializer.errors["non_field_errors"][0]
    )

    serializer = FarmSerializer(
        data={
            "name": "Bad bbox",
            "bbox_south": 0.0,
            "bbox_west": 37.0,
            "bbox_north": 1.0,
            "bbox_east": 36.0,
        }
    )
    assert not serializer.is_valid()
    assert (
        "bbox_west must be < bbox_east."
        in serializer.errors["non_field_errors"][0]
    )

    serializer = FarmSerializer(
        data={"name": "Bad centroid", "centroid_lat": 0.1}
    )
    assert not serializer.is_valid()
    assert "Centroid requires both" in serializer.errors["non_field_errors"][0]


@pytest.mark.django_db
def test_farm_serializer_rejects_duplicate_name() -> None:
    password = secrets.token_urlsafe(12)
    user = get_user_model().objects.create_user(
        username="dup-name",
        email="dup-name@example.com",
        password=password,
    )
    Farm.objects.create(owner=user, name="My Farm", slug="my-farm")
    factory = APIRequestFactory()
    drf_request = Request(factory.post("/"))
    drf_request.user = user

    serializer = FarmSerializer(
        data={"name": "My Farm"},
        context={"request": drf_request},
    )

    assert not serializer.is_valid()
    assert serializer.errors["name"][0] == "Farm name already exists."


@pytest.mark.django_db
def test_farm_serializer_rejects_slug_conflict() -> None:
    password = secrets.token_urlsafe(12)
    user = get_user_model().objects.create_user(
        username="dup-slug",
        email="dup-slug@example.com",
        password=password,
    )
    Farm.objects.create(owner=user, name="My Farm", slug="my-farm")
    factory = APIRequestFactory()
    drf_request = Request(factory.post("/"))
    drf_request.user = user

    serializer = FarmSerializer(
        data={"name": "My Farm!"},
        context={"request": drf_request},
    )

    assert not serializer.is_valid()
    assert (
        serializer.errors["name"][0]
        == "Farm name conflicts with an existing slug."
    )


@pytest.mark.django_db
def test_is_farm_owner_permission() -> None:
    password = secrets.token_urlsafe(12)
    user = get_user_model().objects.create_user(
        username="perm-user",
        email="perm@example.com",
        password=password,
    )
    farm = Farm.objects.create(owner=user, name="Farm", slug="farm")
    permission = IsFarmOwner()
    view = APIView()
    factory = APIRequestFactory()
    drf_request = Request(factory.get("/"))
    drf_request.user = user

    assert permission.has_object_permission(drf_request, view, farm)
    assert not permission.has_object_permission(drf_request, view, "x")


@pytest.mark.django_db
def test_farm_viewset_queryset_anonymous() -> None:
    view = FarmViewSet()
    factory = APIRequestFactory()
    drf_request = Request(factory.get("/"))
    drf_request.user = AnonymousUser()
    view.request = drf_request
    assert list(view.get_queryset()) == []
