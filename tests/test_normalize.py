"""Original columns stay as in the source; normalised values go to extra '(filled)' columns."""
import pytest

openpyxl = pytest.importorskip("openpyxl")

from autoeda import config  # noqa: E402
from autoeda.ingest.excel import ingest_excel  # noqa: E402
from autoeda.profiling import profile_table  # noqa: E402


def _book(path, rows, merges=()):
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    for m in merges:
        ws.merge_cells(m)
    wb.save(path)


def _cols(p):
    return {c["name"]: c for c in p["columns"]}


def test_merged_cells_keep_original_and_add_filled_column(tmp_path):
    _book(tmp_path / "m.xlsx", [["Region", "Item", "Sales"], ["West", "a", 1], [None, "b", 2], [None, "c", 3],
                                ["East", "d", 4], [None, "e", 5]], merges=["A2:A4", "A5:A6"])
    t = ingest_excel(tmp_path / "m.xlsx", tmp_path, "m")[0]
    assert t.columns == ["Region", "Item", "Sales", "Region (filled)"]
    assert t.derived == {"Region (filled)": "Region"}
    c = _cols(profile_table(t))
    assert c["Region"]["missing"] == 3 and c["Region (filled)"]["missing"] == 0
    assert c["Region (filled)"]["derived_from"] == "Region"
    assert c["Region"]["sample"]["head"][1] == [2, None]
    assert [v for _, v in c["Region (filled)"]["sample"]["head"]] == ["West", "West", "West", "East", "East"]


def test_unmerged_group_labels_are_carried_down_in_extra_column(tmp_path):
    rows = [["Group", "Item", "Qty"]]
    for g in ("Fruit", "Veg", "Dairy"):
        rows += [[g, f"{g}1", 1], [None, f"{g}2", 2], [None, f"{g}3", 3]]
    _book(tmp_path / "g.xlsx", rows)
    t = ingest_excel(tmp_path / "g.xlsx", tmp_path, "g")[0]
    assert "Group (filled)" in t.columns
    c = _cols(profile_table(t))
    assert c["Group"]["missing"] == 6 and c["Group (filled)"]["missing"] == 0
    assert [v for _, v in c["Group (filled)"]["sample"]["head"]][:4] == ["Fruit", "Fruit", "Fruit", "Veg"]


def test_plain_sparse_column_is_not_filled(tmp_path):
    rows = [["Id", "Name", "Note"]] + [[i, f"n{i}", "x" if i == 3 else None] for i in range(1, 12)]
    _book(tmp_path / "p.xlsx", rows)
    t = ingest_excel(tmp_path / "p.xlsx", tmp_path, "p")[0]
    assert t.columns == ["Id", "Name", "Note"] and not t.derived


def test_head_tail_sample_and_issue_rows(tmp_path):
    rows = [["Id", "Name"]] + [[i, " pad " if i == 7 else f"n{i}"] for i in range(1, 41)]
    _book(tmp_path / "s.xlsx", rows)
    t = ingest_excel(tmp_path / "s.xlsx", tmp_path, "s")[0]
    c = _cols(profile_table(t))
    s = c["Id"]["sample"]
    assert [r for r, _ in s["head"]] == list(range(1, 11)) and [r for r, _ in s["tail"]] == list(range(31, 41))
    assert s["tail"][-1][1] == "40"
    assert c["Name"]["issue_rows"]["whitespace"][0] == [7, " pad "]


def test_missing_patterns_show_columns_empty_together(tmp_path):
    rows = [["A", "B", "C", "D"]] + [[i, None if i % 4 == 0 else 1, None if i % 4 == 0 else 2, i] for i in range(1, 41)]
    _book(tmp_path / "mp.xlsx", rows)
    t = ingest_excel(tmp_path / "mp.xlsx", tmp_path, "mp")[0]
    mp = profile_table(t)["missing_patterns"]
    assert mp and mp["cols"][:2] in (["B", "C"], ["C", "B"])
    assert any({a, b} == {"B", "C"} for a, b, _ in mp["together"])


def test_repair_log_keeps_row_numbers(tmp_path):
    _book(tmp_path / "r.xlsx", [["A", "B", "C"], ["x", 1, 2], ["y", 3, 4]])
    from autoeda.models import RepairLog
    log = RepairLog()
    for n in (5, 9, 12):
        log.add("repeated_header", n)
    assert log.to_dict()["items"][0]["lines"] == [5, 9, 12]


def test_full_run_exports_cleaned_table_and_renders(tmp_path, monkeypatch):
    for k, d in (("WORK_DIR", "work"), ("REPORT_DIR", "reports")):
        monkeypatch.setattr(config, k, tmp_path / d)
    config.ensure_dirs()
    _book(tmp_path / "f.xlsx", [["Region", "Item", "Sales"], ["West", "a", 1], [None, "b", 2], [None, "c", 3],
                                ["East", "d", 4], [None, "e", 5]], merges=["A2:A4", "A5:A6"])
    from autoeda import pipeline
    out = config.REPORT_DIR / "abc123abc123.html"
    pipeline.run(tmp_path / "f.xlsx", out, job_id="abc123abc123", use_llm=False, report_id="abc123abc123")
    import duckdb
    rows = duckdb.sql(f"select * from read_parquet('{(config.REPORT_DIR / 'abc123abc123_data' / 'table0.parquet').as_posix()}')").fetchall()
    assert len(rows) == 5 and [r[3] for r in rows] == ["West", "West", "West", "East", "East"] and rows[1][0] is None
    html = out.read_text(encoding="utf-8")
    assert "Region (filled)" in html and "Sample rows" in html and "/data/0?fmt=csv" in html


def test_download_endpoint_serves_csv_and_parquet(tmp_path, monkeypatch):
    for k, d in (("WORK_DIR", "work"), ("REPORT_DIR", "reports")):
        monkeypatch.setattr(config, k, tmp_path / d)
    monkeypatch.setattr(config, "AUTH_ENABLED", False)
    config.ensure_dirs()
    _book(tmp_path / "f.xlsx", [["Region", "Item"], ["West", "a"], [None, "b"], [None, "c"], ["East", "d"], [None, "e"],
                                [None, "f"]], merges=["A2:A4"])
    from autoeda import pipeline
    pipeline.run(tmp_path / "f.xlsx", config.REPORT_DIR / "abc123abc123.html", job_id="abc123abc123", use_llm=False)
    from fastapi.testclient import TestClient

    from autoeda import web
    monkeypatch.setitem(web._jobs, "abc123abc123", {"id": "abc123abc123", "file": "f.xlsx", "status": "done", "owner": "x"})
    monkeypatch.setattr(web, "_user", lambda request: {"name": "x", "role": "admin"})
    cl = TestClient(web.app)
    r = cl.get("/reports/abc123abc123/data/0?fmt=csv")
    assert r.status_code == 200 and r.text.splitlines()[0] == "Region,Item,Region (filled)"
    assert cl.get("/reports/abc123abc123/data/0?fmt=parquet").content[:4] == b"PAR1"
    assert cl.get("/reports/abc123abc123/data/5?fmt=csv").status_code == 404
