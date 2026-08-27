from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_gitignore_covers_live_secrets_and_db():
    gi = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for needle in (".env", "crm.db", "companies_house_api_key.txt", "backup.json"):
        assert needle in gi, f"{needle} must stay gitignored"


def test_env_example_is_placeholders_only():
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "sk-" not in example
    # Live connection strings must stay commented placeholders.
    for line in example.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        assert "postgresql://" not in stripped
        assert "ACCOUNT-SECRET" not in stripped
