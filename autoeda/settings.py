"""Settings changed in the app (Settings page). They override the environment defaults of
config.py and are kept in DATA_DIR/settings.json, so they survive restarts.

Each entry: config attribute, type, group, labels and help (en, id). Values are applied by
setting the attribute on the config module; the code reads config.X when it needs it, so a
change takes effect with the next job (restart=True: only after a restart).
"""
from __future__ import annotations

import json
import threading

from . import config

SCHEMA = [
    # --- EDA features ------------------------------------------------------------------------
    {"key": "EDA_DESCRIPTIVE", "type": "bool", "group": "eda",
     "label": ("Descriptive statistics", "Statistik deskriptif"),
     "help": ("One row per column like pandas describe(): count, missing, unique, top, mean, std, quartiles.",
              "Satu baris per kolom seperti describe() di pandas: jumlah, kosong, unik, terbanyak, rata-rata, "
              "simpangan baku, kuartil.")},
    {"key": "EDA_UNIVARIATE", "type": "bool", "group": "eda",
     "label": ("Univariate analysis", "Analisis univariat"),
     "help": ("Per column: distribution chart, most common values, skew and outliers, a plain-language summary.",
              "Per kolom: grafik sebaran, nilai paling sering, kemencengan dan pencilan, ringkasan dalam bahasa "
              "sehari-hari.")},
    {"key": "EDA_MULTIVARIATE", "type": "bool", "group": "eda",
     "label": ("Multivariate analysis", "Analisis multivariat"),
     "help": ("Relationships between columns: strongly correlated numeric columns.",
              "Hubungan antar kolom: kolom angka yang berkorelasi kuat.")},
    {"key": "EDA_QUALITY", "type": "bool", "group": "eda",
     "label": ("Data quality checks", "Pemeriksaan kualitas data"),
     "help": ("Problems in the data (empty columns, mixed formats, placeholder values, unusual values...), the "
              "quality score and the verdict.",
              "Masalah pada data (kolom kosong, format campur, nilai pengganti, nilai tak wajar...), skor "
              "kualitas dan kesimpulan.")},
    {"key": "EDA_DUPLICATES", "type": "bool", "group": "eda",
     "label": ("Duplicate rows", "Baris ganda"),
     "help": ("Count rows that appear more than once.", "Hitung baris yang muncul lebih dari sekali.")},
    # --- AI ------------------------------------------------------------------------------------
    {"key": "AI_DEFAULT_ON", "type": "bool", "group": "ai",
     "label": ("Use AI by default", "Pakai AI secara bawaan"),
     "help": ("The 'Use AI' switch on the start page is on when the model is reachable.",
              "Tombol 'Pakai AI' di halaman awal menyala jika model tersedia.")},
    {"key": "AI_TABLE_LAYOUT", "type": "bool", "group": "ai",
     "label": ("AI helps read difficult tables", "AI membantu membaca tabel sulit"),
     "help": ("Header depth, page breaks and borderless PDF layouts the rules are unsure about.",
              "Kedalaman judul kolom, pergantian halaman, dan tata letak PDF tanpa garis yang meragukan.")},
    {"key": "AI_DESCRIPTIONS", "type": "bool", "group": "ai",
     "label": ("AI column descriptions", "Deskripsi kolom oleh AI"), "help": ("", "")},
    {"key": "AI_SUMMARY", "type": "bool", "group": "ai",
     "label": ("AI summary of each table", "Ringkasan tabel oleh AI"), "help": ("", "")},
    {"key": "AI_ANOMALY_REVIEW", "type": "bool", "group": "ai",
     "label": ("AI anomaly review", "Tinjauan anomali oleh AI"),
     "help": ("The model looks for odd values the rules cannot judge (typos, wrong categories).",
              "Model mencari nilai janggal yang tidak bisa dinilai aturan (salah ketik, kategori keliru).")},
    {"key": "OLLAMA_MODEL", "type": "text", "group": "ai", "label": ("Default model", "Model bawaan"),
     "help": ("Ollama model name, e.g. qwen3.5:2b.", "Nama model Ollama, mis. qwen3.5:2b.")},
    {"key": "OLLAMA_HOST", "type": "text", "group": "ai", "label": ("Ollama address", "Alamat Ollama"),
     "help": ("", "")},
    {"key": "LLM_TIMEOUT", "type": "int", "group": "ai", "min": 10, "max": 3600,
     "label": ("AI answer timeout (seconds)", "Batas waktu jawaban AI (detik)"), "help": ("", "")},
    {"key": "LLM_BATCH", "type": "int", "group": "ai", "min": 1, "max": 50,
     "label": ("Columns per AI request", "Kolom per permintaan AI"), "help": ("", "")},
    {"key": "LLM_MAX_COLUMNS", "type": "int", "group": "ai", "min": 0, "max": 2000,
     "label": ("AI descriptions up to this many columns", "Deskripsi AI sampai sebanyak ini kolom"),
     "help": ("Wider tables get rule-based descriptions only.", "Tabel yang lebih lebar hanya mendapat "
                                                                "deskripsi berbasis aturan.")},
    # --- memory --------------------------------------------------------------------------------
    {"key": "MEMORY_ENABLED", "type": "bool", "group": "memory",
     "label": ("Learn from comments and use skills", "Belajar dari komentar dan pakai keterampilan"),
     "help": ("Off: comments are not asked for and learned skills are not applied (they are kept).",
              "Mati: komentar tidak diminta dan keterampilan tidak dipakai (tetap disimpan).")},
    # --- files ---------------------------------------------------------------------------------
    {"key": "KEEP_UPLOADS", "type": "bool", "group": "files",
     "label": ("Keep uploaded files after the report", "Simpan file unggahan setelah laporan selesai"),
     "help": ("Needed for 'Run again'. Off: a file is deleted as soon as its report is ready, to save space. "
              "Single files can also be deleted from the report list.",
              "Diperlukan untuk 'Jalankan lagi'. Mati: file dihapus begitu laporannya siap, untuk menghemat "
              "ruang. File satu per satu juga bisa dihapus dari daftar laporan.")},
    {"key": "ENGINE", "type": "choice", "group": "files", "choices": ["local", "spark"],
     "label": ("Default engine", "Mesin bawaan"), "help": ("", "")},
    # --- PDF -----------------------------------------------------------------------------------
    {"key": "PDF_ENGINE", "type": "choice", "group": "pdf", "choices": ["auto", "plumber", "docling"],
     "label": ("PDF table reader", "Pembaca tabel PDF"),
     "help": ("auto: geometry first, Docling for borderless pages; plumber: geometry only; docling: always.",
              "auto: geometri dulu, Docling untuk halaman tanpa garis; plumber: geometri saja; docling: selalu.")},
    {"key": "DOCLING_MAX_PAGES", "type": "int", "group": "pdf", "min": 0, "max": 5000,
     "label": ("Docling page limit per file", "Batas halaman Docling per file"), "help": ("", "")},
    {"key": "OCR_LANG", "type": "text", "group": "pdf", "label": ("OCR language(s)", "Bahasa OCR"),
     "help": ("Tesseract languages for scanned pages, e.g. eng+ind.",
              "Bahasa Tesseract untuk halaman pindaian, mis. eng+ind.")},
    # --- performance ---------------------------------------------------------------------------
    {"key": "DUCKDB_MEMORY_LIMIT", "type": "text", "group": "perf",
     "label": ("Working memory", "Memori kerja"), "help": ("e.g. 4GB", "mis. 4GB")},
    {"key": "DUCKDB_THREADS", "type": "int", "group": "perf", "min": 1, "max": 256,
     "label": ("Threads", "Thread"), "help": ("", "")},
    {"key": "SAMPLE_ROWS", "type": "int", "group": "perf", "min": 1000, "max": 10_000_000,
     "label": ("Rows sampled for type detection", "Baris sampel untuk deteksi tipe"), "help": ("", "")},
    {"key": "HUGE_ROWS", "type": "int", "group": "perf", "min": 100_000, "max": 10_000_000_000,
     "label": ("Approximate statistics above this many rows", "Statistik perkiraan di atas sekian baris"),
     "help": ("", "")},
    # --- Spark cluster and HDFS on the host ----------------------------------------------------
    {"key": "SPARK_MASTER", "type": "text", "group": "spark",
     "label": ("Spark master", "Spark master"),
     "help": ("The cluster already running on the host: spark://host:7077 (standalone), yarn, or local[*]. "
              "Used from the next Spark job on; nothing is installed or started.",
              "Klaster yang sudah berjalan di host: spark://host:7077 (standalone), yarn, atau local[*]. "
              "Dipakai mulai proses Spark berikutnya; tidak ada yang dipasang atau dijalankan.")},
    {"key": "SPARK_CONF", "type": "text", "group": "spark", "optional": True, "max_len": 4000,
     "label": ("Spark configuration", "Konfigurasi Spark"),
     "help": ("key=value pairs separated by ';', e.g. spark.executor.memory=48g;spark.cores.max=210. "
              "Size them to the resources shown above.",
              "Pasangan key=value dipisah ';', mis. spark.executor.memory=48g;spark.cores.max=210. "
              "Sesuaikan dengan sumber daya yang ditampilkan di atas.")},
    {"key": "SPARK_UI_URL", "type": "text", "group": "spark", "optional": True,
     "label": ("Cluster web UI / REST address", "Alamat web UI / REST klaster"),
     "help": ("Where the cluster's resources are read: standalone master http://host:8080, YARN "
              "ResourceManager http://host:8088. Empty: derived from the master.",
              "Tempat membaca sumber daya klaster: master standalone http://host:8080, YARN ResourceManager "
              "http://host:8088. Kosong: diturunkan dari master.")},
    {"key": "SPARK_WORK_DIR", "type": "text", "group": "spark",
     "label": ("Spark work folder", "Folder kerja Spark"),
     "help": ("Every executor and this app must reach it at the same path: hdfs:///tmp/autoeda or a shared "
              "file:/// mount.", "Semua executor dan aplikasi ini harus bisa menjangkaunya di path yang sama: "
              "hdfs:///tmp/autoeda atau mount bersama file:///.")},
    {"key": "HDFS_URL", "type": "text", "group": "spark", "optional": True,
     "label": ("WebHDFS address", "Alamat WebHDFS"),
     "help": ("NameNode REST address, e.g. http://namenode:9870. Lets users browse HDFS and analyse any "
              "HDFS file (Excel, PDF... are copied into the app first).",
              "Alamat REST NameNode, mis. http://namenode:9870. Pengguna dapat menelusuri HDFS dan menganalisis "
              "file HDFS apa pun (Excel, PDF... disalin ke aplikasi dulu).")},
    {"key": "HDFS_ROOT", "type": "text", "group": "spark", "optional": True,
     "label": ("HDFS file system name", "Nama file system HDFS"),
     "help": ("Prefix of picked files, e.g. hdfs://nameservice1 (fs.defaultFS). Empty: hdfs:/// (the default).",
              "Awalan file yang dipilih, mis. hdfs://nameservice1 (fs.defaultFS). Kosong: hdfs:/// (bawaan).")},
    {"key": "HDFS_USER", "type": "text", "group": "spark", "optional": True,
     "label": ("HDFS user", "Pengguna HDFS"),
     "help": ("user.name for WebHDFS (simple authentication).", "user.name untuk WebHDFS (autentikasi sederhana).")},
    {"key": "MAX_CONCURRENT_JOBS", "type": "int", "group": "perf", "min": 1, "max": 64, "restart": True,
     "label": ("Analyses at the same time", "Analisis bersamaan"),
     "help": ("Takes effect after a restart.", "Berlaku setelah aplikasi dijalankan ulang.")},
]
GROUPS = {"eda": ("EDA features", "Fitur EDA"), "ai": ("AI", "AI"), "memory": ("Memory", "Memori"),
          "files": ("Files and jobs", "File dan proses"), "pdf": ("PDF", "PDF"),
          "spark": ("Spark cluster and HDFS", "Klaster Spark dan HDFS"), "perf": ("Performance", "Kinerja")}
_KEYS = {s["key"]: s for s in SCHEMA}
_lock = threading.Lock()
DEFAULTS = {s["key"]: getattr(config, s["key"]) for s in SCHEMA}     # environment / built-in values


def _path():
    return config.DATA_DIR / "settings.json"


def saved() -> dict:
    try:
        return json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def values() -> dict:
    return {k: getattr(config, k) for k in _KEYS}


def apply():
    """Put the saved values on the config module (call before a job and at start-up)."""
    for k, v in saved().items():
        if k in _KEYS:
            setattr(config, k, v)


def _check(spec: dict, v):
    t = spec["type"]
    if t == "bool":
        if not isinstance(v, bool):
            raise ValueError(f"{spec['key']}: true or false")
        return v
    if t == "int":
        if isinstance(v, bool) or not isinstance(v, (int, float, str)) or not str(v).strip().lstrip("-").isdigit():
            raise ValueError(f"{spec['key']}: a whole number")
        v = int(str(v).strip())
        if not spec.get("min", v) <= v <= spec.get("max", v):
            raise ValueError(f"{spec['key']}: between {spec.get('min')} and {spec.get('max')}")
        return v
    if t == "choice":
        if v not in spec["choices"]:
            raise ValueError(f"{spec['key']}: one of {', '.join(spec['choices'])}")
        return v
    v = str(v).strip()
    if not v and not spec.get("optional"):
        raise ValueError(f"{spec['key']}: empty")
    return v[:spec.get("max_len", 300)]


def save(changes: dict) -> dict:
    """Validate and store changed values; a value equal to the default is not stored."""
    with _lock:
        cur = saved()
        for k, v in changes.items():
            spec = _KEYS.get(k)
            if spec is None:
                raise ValueError(f"Unknown setting: {k}")
            v = _check(spec, v)
            if v == DEFAULTS[k]:
                cur.pop(k, None)
            else:
                cur[k] = v
            setattr(config, k, v)
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        _path().write_text(json.dumps(cur, indent=1, sort_keys=True), encoding="utf-8")
    return values()


def reset() -> dict:
    with _lock:
        _path().unlink(missing_ok=True)
        for k, v in DEFAULTS.items():
            setattr(config, k, v)
    return values()


def describe(lang: str = "en") -> list[dict]:
    i = 0 if lang == "en" else 1
    cur = values()
    return [{"key": s["key"], "type": s["type"], "group": s["group"], "group_label": GROUPS[s["group"]][i],
             "label": s["label"][i], "help": s["help"][i], "choices": s.get("choices"), "min": s.get("min"),
             "max": s.get("max"), "restart": s.get("restart", False), "value": cur[s["key"]],
             "default": DEFAULTS[s["key"]]} for s in SCHEMA]
