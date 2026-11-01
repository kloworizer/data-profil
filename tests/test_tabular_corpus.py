"""Excel and CSV files from Indonesian open-data portals (tests/data/tabular, sources in
SOURCES.md): hand-made government spreadsheets with titles, indicator rows and sub-rows,
merged multi-level headers, #REF! errors, empty sheets; CSV exports with '.' as thousands
separator next to plain integers."""
from __future__ import annotations

from pathlib import Path

import pyarrow.parquet as pq
import pytest

from autoeda.ingest import ingest
from autoeda.profiling import profile_table

DATA = Path(__file__).parent / "data" / "tabular"


def read(name, tmp):
    tables, _ = ingest(DATA / name, tmp)
    return {t.name: t for t in tables}


def rows(t):
    cols = [c for c in t.columns if c not in t.drop_columns]
    return cols, [["" if r[c] is None else str(r[c]) for c in cols] for r in pq.read_table(t.path, columns=cols).to_pylist()]


def col(profile, name):
    return next(c for c in profile["columns"] if c["name"] == name)


def test_indicator_sheet_keeps_jumlah_rows(tmp_path):
    """'Jumlah Kabupaten ...' (number of districts) is an indicator, not a total row."""
    t = read("id_kaltim_bencana_alam.xlsx", tmp_path)
    cols, data = rows(t["Bencana Alam"])
    assert cols == ["No", "Kode Referensi", "Daftar Data", "2021", "2022", "2023", "2024", "2025", "Satuan"]
    assert len(data) == 21
    assert [v.strip() for v in data[1][:4]] == ["", "6400", "(1) Banjir", "1708"]     # cell has a trailing space
    assert sum(r[2].startswith("Jumlah") for r in data) == 11
    assert len(rows(t["Bencana Alam (2)"])[1]) == 20


@pytest.mark.xfail(strict=True, reason="three-level merged header (JENIS BENCANA / Kecelakaan Transportasi / Udara)")
def test_three_level_merged_header(tmp_path):
    cols, data = rows(read("id_kaltim_bencana_alam.xlsx", tmp_path)["Sheet1"])
    assert "JENIS BENCANA / Kecelakaan Transportasi / Udara" in cols
    assert len(data) == 10 and data[0][:2] == ["1", "Samarinda"]


@pytest.mark.parametrize("name, n, first", [
    ("id_kaltim_disabilitas_sekolah.xlsx", 2, ["375"]),
    ("id_kaltim_lulusan.xlsx", 15, ["1"]),
    ("id_kaltim_dprd.xlsx", 7, ["1", "Jumlah Anggota DPRD", "385"]),
    ("id_kaltim_batas_negara.xlsx", 2, ["1", "Indonesia"]),       # two empty sheets beside it
    ("id_kaltim_air_baku.xlsx", 1, ["1", "6400", "Kapasitas Air Baku", "7922"]),   # table starts in column B
])
def test_small_government_sheets(tmp_path, name, n, first):
    tables = list(read(name, tmp_path).values())
    assert len(tables) == 1
    _, data = rows(tables[0])
    assert len(data) == n and data[0][:len(first)] == first


def test_dot_thousands_beside_integers(tmp_path):
    """397.278 people next to 9002 people: '.' separates thousands."""
    t = list(read("id_samarinda_penduduk_pendidikan.csv", tmp_path).values())[0]
    assert t.n_rows == 22
    p = profile_table(t)
    c = col(p, "2018")
    assert c["inferred_type"] == "integer" and c["numeric"]["max"] == 397278
    # '47.19' (trailing zero dropped) is read as 47,190 and pointed out
    assert any(f["code"] == "thousands_ambiguous" for f in col(p, "2019")["flags"])
    kor = profile_table(list(read("id_kaltim_kor.csv", tmp_path).values())[0])
    assert col(kor, "2022")["numeric"]["max"] == 30418


def test_dot_everywhere_is_pointed_out(tmp_path):
    """Every value like 55.315: decimals or thousands cannot be told apart; the report says so."""
    p = profile_table(list(read("id_samarinda_penduduk_usia.csv", tmp_path).values())[0])
    c = col(p, "2017")
    assert c["inferred_type"] == "float" and any(f["code"] == "maybe_thousands" for f in c["flags"])


def test_long_csv(tmp_path):
    t = list(read("id_samarinda_usia_tunggal.csv", tmp_path).values())[0]
    assert t.n_rows == 231 and len(t.columns) == 10
    p = profile_table(t)
    assert col(p, "2022")["numeric"]["max"] == 849717
