from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_client_save_does_not_assign_gateway_passwords():
    src = (ROOT / "app" / "routers" / "clients.py").read_text(encoding="utf-8")
    assert "client.gov_gateway_password =" not in src
    assert "client.accounts_software_password =" not in src


def test_person_save_does_not_assign_gateway_passwords():
    src = (ROOT / "app" / "routers" / "people.py").read_text(encoding="utf-8")
    assert "person.gov_gateway_password =" not in src
    assert "gov_gateway_password=" not in src


def test_client_form_does_not_echo_passwords_in_html():
    html = (ROOT / "app" / "templates" / "clients" / "detail.html").read_text(encoding="utf-8")
    assert 'name="gov_gateway_password"' not in html
    assert 'name="accounts_software_password"' not in html
    assert "client.gov_gateway_password or ''" not in html


def test_people_csv_does_not_export_passwords():
    src = (ROOT / "app" / "services" / "csv_exchange.py").read_text(encoding="utf-8")
    assert "gov_gateway_password" not in src
