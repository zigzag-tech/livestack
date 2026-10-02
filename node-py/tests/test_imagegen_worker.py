from fastapi.testclient import TestClient
from livestack_node.imagegen import worker


def test_request_holds_lease_during_admission_and_releases_on_refusal(tmp_path, monkeypatch):
    token = tmp_path / "token"; token.write_text("secret")
    active = []
    class Coordinator:
        def acquire_lease(self, *args, **kwargs):
            active.append("lease")
            return {"lease_id": "lease"}
        def release_lease(self, lease_id):
            active.remove(lease_id)
    monkeypatch.setattr(worker, "attach", lambda *args, **kwargs: (None, Coordinator()))
    def refused(*args, **kwargs):
        assert active == ["lease"], "protect the image unit before broker warm can return"
        return {"granted": False, "reason": "capacity busy"}
    monkeypatch.setattr(worker, "admit", refused)
    config = {"token_file": str(token), "params_b": 6, "model": "Tongyi-MAI/Z-Image-Turbo", "unit": "z", "resident_bytes": 1, "host_id": "joe", "port": 8210, "device_id": "joe/gpu", "broker": "http://broker"}
    client = TestClient(worker.create_app(config))
    response = client.post("/v1/images/generations", json={"prompt": "apple"}, headers={"Authorization": "Bearer secret"})
    assert response.status_code == 503
    assert active == []


def test_worker_rejects_resolution_above_qualified_limit(tmp_path, monkeypatch):
    token = tmp_path / "token"; token.write_text("secret")
    monkeypatch.setattr(worker, "attach", lambda *a, **k: (None, None))
    config = {"token_file": str(token), "params_b": 6, "model": "Tongyi-MAI/Z-Image-Turbo", "unit": "z", "resident_bytes": 1, "host_id": "joe", "port": 8210, "device_id": "joe/gpu", "broker": "http://broker", "max_dimension": 768}
    response = TestClient(worker.create_app(config)).post("/v1/images/generations", json={"prompt": "apple", "width": 1024, "height": 1024}, headers={"Authorization": "Bearer secret"})
    assert response.status_code == 422
    assert "768" in response.json()["detail"]
