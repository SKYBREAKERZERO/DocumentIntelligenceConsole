"""Backend regression: an explicit project scope may not silently query QA/BUG."""
from fastapi.testclient import TestClient

from app.server.api import create_app


class FakeRuntime:
    class Service:
        retriever = object()

    preferred_model = "local-model"

    def get_service(self):
        return self.Service()

    def local_status(self):
        return {"mode": "LOCAL_ONLY"}


def test_explicit_spec_scope_cannot_use_qa_bug_route():
    api = TestClient(create_app(FakeRuntime(), "a" * 64), base_url="http://localhost")
    response = api.post(
        "/api/v1/retrieval",
        headers={"Authorization": "Bearer " + "a" * 64},
        json={"scope": "21MM", "question": "Bluetooth Issue", "operation": "qa_bug"},
    )
    assert response.status_code == 400
    assert "QA/BUG operation requires" in response.json()["detail"]


def test_intelligence_api_auth_is_required():
    api = TestClient(create_app(FakeRuntime(), "a" * 64), base_url="http://localhost")
    response = api.get("/api/v1/intelligence/unresolved-anchors?scope=21MM&spec_code=508")
    assert response.status_code == 401


def test_model_listing_does_not_depend_on_backend_readiness():
    api = TestClient(create_app(FakeRuntime(), "a" * 64), base_url="http://localhost")
    response = api.get("/v1/models", headers={"Authorization": "Bearer " + "a" * 64})
    assert response.status_code == 200
    assert response.json()["data"][0]["id"] == "document-intelligence-rag"
