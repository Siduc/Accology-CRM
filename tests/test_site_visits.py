LOGIN = {"username": "teststaff", "password": "test-password-not-live"}


def _visit_count():
    from app.database import SessionLocal
    from app.models.site_visit import SiteVisit

    db = SessionLocal()
    n = db.query(SiteVisit).count()
    db.close()
    return n


def test_public_home_counts_a_browser_hit(client):
    client.get("/logout")
    before = _visit_count()
    r = client.get(
        "/",
        headers={"User-Agent": "Mozilla/5.0", "Host": "accology.co"},
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert _visit_count() == before + 1


def test_bot_and_loopback_are_skipped(client):
    from app.services.site_visits import _is_loopback

    assert _is_loopback("127.0.0.1")
    client.get("/logout")
    before = _visit_count()
    client.get(
        "/",
        headers={"User-Agent": "Googlebot/2.1", "Host": "accology.co"},
        follow_redirects=False,
    )
    assert _visit_count() == before


def test_staff_hub_shows_counter(client):
    client.get("/logout")
    login = client.post("/login", data=LOGIN, follow_redirects=False)
    assert login.status_code in (302, 303)
    hub = client.get("/prospecting")
    assert hub.status_code == 200
    assert "accology.co visits" in hub.text
    log = client.get("/prospecting/site-visits")
    assert log.status_code == 200
    assert "No cookies" in log.text
    client.get("/logout")
