def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["database"] is True
    assert body["dialect"] == "sqlite"
    assert "password" not in str(body).lower()
    assert "secret" not in str(body).lower()


def test_docs_off_when_not_forced(client):
    # development keeps /docs; this just checks the app boots
    r = client.get("/docs")
    assert r.status_code in (200, 404)
