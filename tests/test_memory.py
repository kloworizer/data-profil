"""Memory: comments become skills, skills come back for files of the same kind and
change how they are read."""
from __future__ import annotations

import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from autoeda import config, memory
from autoeda.models import Table

CTX = {"file": "Pengumuman Mutasi Pegawai.pdf", "format": "pdf", "table": "table_2 (p.8)", "where": "8-82",
       "rows": 1014, "text": "LAMPIRAN II DAFTAR PEGAWAI MUTASI DAN PENGANGKATAN",
       "columns": ["NO", "NAMA/NIP", "JABATAN LAMA", "JABATAN BARU", "KEHADIRAN"]}


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path / "memory")


class FakeLLM:
    def __init__(self, reply):
        self.reply, self.prompts = reply, []

    def chat(self, prompt, **kw):
        self.prompts.append(prompt)
        return self.reply if isinstance(self.reply, str) else json.dumps(self.reply)


def test_rules_read_common_requests():
    h = memory.parse_comment("The header has 2 rows; table 2 and 3 are one table. NAMA/NIP is an ID", CTX)["hints"]
    assert h == {"header_rows": 2, "join_tables": True, "column_types": {"NAMA/NIP": "identifier"}}
    h = memory.parse_comment("judul kolom ada 2 baris, hapus baris berisi 'Ditandatangani'", CTX)["hints"]
    assert h["header_rows"] == 2 and h["drop_rows"] == ["Ditandatangani"]
    h = memory.parse_comment('rename column "KEHADIRAN" to "Attendance"', CTX)["hints"]
    assert h["rename_columns"] == {"KEHADIRAN": "Attendance"}


def test_learn_without_model_then_refine():
    a = memory.learn("table 2 and 3 are one table", CTX)
    assert a["action"] == "create" and a["skill"]["hints"] == {"join_tables": True}
    b = memory.learn("also the header has 2 rows", {**CTX, "file": "Pengumuman Mutasi Oktober.pdf"})
    assert b["action"] == "update" and b["skill"]["id"] == a["skill"]["id"]      # same kind of file: refined
    s = memory.load()
    assert len(s) == 1 and s[0]["hints"] == {"join_tables": True, "header_rows": 2}
    assert s[0]["version"] == 2 and len(s[0]["sources"]) == 2
    assert [f["action"] for f in memory.feedback_log()] == ["update", "create"]


def test_learn_with_model_keeps_parsed_numbers():
    llm = FakeLLM({"action": "create", "title": "DJP lampiran tables", "formats": ["pdf"],
                   "instruction": "In DJP announcement annexes every Lampiran is a separate table.",
                   "keywords": ["lampiran", "pegawai", "kehadiran"], "hints": {"header_rows": 3, "bogus": 1}})
    out = memory.learn("the header has 2 rows", CTX, llm)
    s = out["skill"]
    assert s["title"] == "DJP lampiran tables" and s["instruction"].startswith("In DJP")
    assert s["hints"] == {"header_rows": 2}          # explicit number from the comment wins, unknown keys dropped
    assert "Existing skills" in llm.prompts[0] and "User comment" in llm.prompts[0]


def test_model_hints_need_support_in_the_comment():
    """A small model likes to add hints nobody asked for; they are dropped."""
    llm = FakeLLM({"action": "update", "skill_id": "nope", "title": "Totals", "instruction": "Village totals are no data.",
                   "keywords": ["village"], "hints": {"header_rows": 1, "join_tables": True,
                                                      "column_types": {"Desa Village": "text"}}})
    ctx = {**CTX, "columns": ["Desa Village", "Banjir Floods"]}
    out = memory.learn("Baris 'Kecamatan Manggelewa' adalah total, bukan data. Kolom Desa Village adalah "
                       "kategori, bukan teks bebas", ctx, llm)
    assert out["action"] == "create"                      # nothing to update yet
    assert out["skill"]["hints"] == {"drop_rows": [r"^Kecamatan\s+Manggelewa$"],
                                     "column_types": {"Desa Village": "category"}}


def test_row_rule_generalises_the_document_subject():
    """'Kecamatan Manggelewa' is the district the book is about: the rule learned from it
    must also drop 'Kecamatan Woja' in the Woja book; 'containing' rules stay unanchored."""
    ctx = {**CTX, "file": "kecamatan_manggelewa_dalam_angka.pdf", "text": "KECAMATAN MANGGELEWA DALAM ANGKA 2024"}
    pat = memory.parse_comment("'Kecamatan Manggelewa' adalah total", ctx)["hints"]["drop_rows"][0]
    assert pat == r"^Kecamatan\s+\S+$"
    pat = memory.parse_comment("remove rows containing 'Ditandatangani'", ctx)["hints"]["drop_rows"][0]
    assert pat == "Ditandatangani"


def test_refining_rewrites_one_instruction():
    memory.learn("table 2 and 3 are one table", CTX)
    llm = FakeLLM({"action": "update", "title": "DJP annexes", "keywords": ["lampiran"], "hints": {},
                   "instruction": "Annex tables continue over pages; their header has 2 rows."})
    out = memory.learn("the header has 2 rows", CTX, llm)
    assert out["action"] == "update"
    assert out["skill"]["instruction"] == "Annex tables continue over pages; their header has 2 rows."
    assert "table 2 and 3 are one table" in llm.prompts[0]      # the model saw the lesson it refines
    assert out["skill"]["hints"] == {"join_tables": True, "header_rows": 2}


def test_bad_model_reply_falls_back_to_rules():
    out = memory.learn("table 2 and 3 are one table", CTX, FakeLLM("not json at all"))
    assert out["skill"]["hints"] == {"join_tables": True}


def test_recall_by_kind_of_file():
    memory.learn("table 2 and 3 are one table", CTX)
    same = {"file": "mutasi november.pdf", "format": "pdf", "text": "", "columns": ["NO", "NAMA/NIP", "KEHADIRAN"]}
    other = {"file": "sales.csv", "format": "csv", "text": "", "columns": ["Region", "Q1", "Q2"]}
    assert len(memory.recall(same)) == 1
    assert memory.recall(other) == []
    sid = memory.load()[0]["id"]
    memory.update_skill(sid, {"enabled": False})
    assert memory.recall(same) == []


def test_prompt_block_lists_instructions():
    memory.learn("NAMA/NIP is an ID", CTX)
    block = memory.prompt_block(memory.load())
    assert block.startswith("Lessons learned") and "NAMA/NIP is an ID" in block


def _table(tmp_path, name, rows, cols=("NO", "NAMA")):
    p = tmp_path / f"{name}.parquet"
    pq.write_table(pa.table({c: [r[i] for r in rows] for i, c in enumerate(cols)}), p)
    return Table(name, p, list(cols), n_rows=len(rows), source={"format": "pdf", "pages": name})


def test_apply_join_and_drop(tmp_path):
    t1 = _table(tmp_path, "1-2", [["1", "Ani"], ["2", "Budi"]])
    t2 = _table(tmp_path, "3-3", [["3", "Citra"], ["Ditandatangani secara elektronik", None]])
    out = memory.apply_to_tables([t1, t2], {"join_tables": True, "drop_rows": ["^Ditandatangani"],
                                            "column_types": {"nama": "identifier"}}, tmp_path)
    assert len(out) == 1 and out[0].n_rows == 3 and out[0].source["pages"] == "1-3"
    assert pq.read_table(out[0].path).column("NAMA").to_pylist() == ["Ani", "Budi", "Citra"]
    assert out[0].type_hints == {"NAMA": "identifier"}


def test_pipeline_uses_skill(tmp_path, monkeypatch):
    """A learned column type changes the profile of the next file of that kind."""
    from autoeda.pipeline import run
    monkeypatch.setattr(config, "WORK_DIR", tmp_path / "work")
    csv = tmp_path / "pegawai_mutasi.csv"
    csv.write_text("NIP,Nama,Golongan\n" + "".join(f"1974041419940310{i:02d},Pegawai {i},III/a\n" for i in range(40)),
                   encoding="utf-8")
    ctx = {"file": csv.name, "format": "text", "columns": ["NIP", "Nama", "Golongan"], "text": ""}
    memory.learn("NIP is an ID, not a number", ctx)
    res = run(csv, tmp_path / "r.html", use_llm=False)
    nip = next(c for c in res["tables"][0]["columns"] if c["name"] == "NIP")
    assert nip["inferred_type"] == "text" and nip["semantic"] == "identifier"
    assert [s["title"] for s in res["skills"]] == ["NIP is an ID, not a number"]
    assert memory.load()[0]["uses"] == 1


def test_synced_file_and_clear():
    """skills.json holds what is learned, not per-run counters; clearing forgets everything."""
    memory.learn("table 2 and 3 are one table", CTX)
    memory.mark_used(memory.load())
    raw = json.loads((config.MEMORY_DIR / "skills.json").read_text(encoding="utf-8"))
    assert "uses" not in raw[0] and memory.load()[0]["uses"] == 1
    assert memory.clear() == {"skills": 1, "feedback": 1}
    assert memory.load() == [] and memory.feedback_log() == []
