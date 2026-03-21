from rest_framework import status
from rest_framework.test import APITestCase


class IntegrationHealthTests(APITestCase):
    def test_health_endpoint_returns_ok(self) -> None:
        """Return the success envelope for GET /api/v1/integrations/health/."""

        response = self.client.get("/api/v1/integrations/health/")
        assert response.status_code == status.HTTP_200_OK
        payload = response.json()
        assert payload["status"] == 0
        assert payload["data"]["ok"] is True
