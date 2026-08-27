from datetime import date
from pathlib import Path

from app.services.iris_elements import parse_tb_file, write_iris_csv
from app.services.working_papers import write_draft_pack


def test_write_draft_pack_xlsx(tmp_path):
    dest = tmp_path / "pack.xlsx"
    lines = [
        {"source": "Sales", "iris": "Turnover - Sales", "amount": 1200.0, "map": "mapped"},
        {"source": "Bank", "iris": "Bank current account", "amount": -1200.0, "map": "mapped"},
    ]
    write_draft_pack(
        dest,
        company="Pack Test Ltd",
        as_at=date(2026, 3, 31),
        source_name="tb.csv",
        lines=lines,
        queries=["Example query"],
    )
    assert dest.is_file()
    assert dest.stat().st_size > 1000


def test_iris_csv_nets_to_zero(tmp_path):
    path = tmp_path / "iris.csv"
    net, unknown = write_iris_csv(
        path,
        "Year End 31/03/2026",
        {"Turnover - Sales": 50.0, "Bank current account": -50.0},
        ["Turnover - Sales", "Bank current account"],
    )
    assert net == 0.0
    assert unknown == []
    text = path.read_text(encoding="utf-8")
    assert "Turnover - Sales" in text
    assert "Net (must be 0.00)" in text


def test_parse_simple_tb_csv(tmp_path):
    p = tmp_path / "tb.csv"
    p.write_text("Account,Debit,Credit\nSales,100,\nBank,,100\n", encoding="utf-8")
    rows = parse_tb_file(p)
    by = {r["source"]: r["amount"] for r in rows}
    assert by["Sales"] == 100.0
    assert by["Bank"] == -100.0
