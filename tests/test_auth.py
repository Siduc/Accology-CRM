def test_login_page_public(client):
    r = client.get("/login")
    assert r.status_code == 200


def test_dashboard_requires_login(client):
    r = client.get("/dashboard", follow_redirects=False)
    assert r.status_code in (303, 307, 401, 403)
    if r.status_code in (303, 307):
        loc = r.headers.get("location") or ""
        assert "/dashboard" not in loc or loc.endswith("/login")


def test_staff_login_reaches_dashboard(client):
    r = client.post(
        "/login",
        data={"username": "teststaff", "password": "test-password-not-live"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "/dashboard" in (r.headers.get("location") or "")


def test_bad_password_rejected(client):
    r = client.post(
        "/login",
        data={"username": "teststaff", "password": "wrong"},
        follow_redirects=False,
    )
    assert r.status_code in (303, 400)
    if r.status_code == 303:
        loc = r.headers.get("location") or ""
        assert "/dashboard" not in loc


def test_api_post_without_key_is_json_401_not_login_redirect(client):
    r = client.post("/api/v1/tasks/from-email", json={}, follow_redirects=False)
    assert r.status_code != 303
    assert r.status_code == 401
    assert r.headers.get("content-type", "").startswith("application/json")
