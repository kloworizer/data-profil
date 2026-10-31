"""Runtime configuration, all overridable through environment variables."""
import os
from pathlib import Path


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


DATA_DIR = Path(os.getenv("AUTOEDA_DATA_DIR", "./data")).resolve()
UPLOAD_DIR = DATA_DIR / "uploads"
WORK_DIR = DATA_DIR / "work"
REPORT_DIR = DATA_DIR / "reports"
# Server-side folder users may profile in place (for files too large to upload).
INPUT_DIR = Path(os.getenv("AUTOEDA_INPUT_DIR", "./input")).resolve()

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3.5:2b")
LLM_TIMEOUT = _int("AUTOEDA_LLM_TIMEOUT", 180)
LLM_BATCH = _int("AUTOEDA_LLM_BATCH", 5)            # columns per LLM call
LLM_MAX_COLUMNS = _int("AUTOEDA_LLM_MAX_COLUMNS", 80)  # beyond this, rule-based text only

# DuckDB resource limits: keep well under a 16 GB machine.
DUCKDB_MEMORY_LIMIT = os.getenv("AUTOEDA_MEMORY_LIMIT", "4GB")
DUCKDB_THREADS = _int("AUTOEDA_THREADS", 4)

SAMPLE_ROWS = _int("AUTOEDA_SAMPLE_ROWS", 100_000)   # rows used for type inference
SAMPLE_CELLS = 20_000_000                             # cap sample size for wide tables
SNIFF_LINES = 2000                                     # lines used to sniff text files
# Dirty text files above this size skip the (slower) full Python repair pass:
# DuckDB parses them natively and only rejected lines are repaired.
NATIVE_PARSE_MIN_BYTES = _int("AUTOEDA_NATIVE_PARSE_MIN_BYTES", 1024 ** 3)
HUGE_ROWS = _int("AUTOEDA_HUGE_ROWS", 20_000_000)   # above: approximate stats only
SLIDE_CHECK =os.getenv("AUTOEDA_SLIDE_CHECK", "1") != "0"
# Engine: "local" (DuckDB, single machine) or "spark" (cluster).
ENGINE = os.getenv("AUTOEDA_ENGINE", "local")
# Where the Spark engine materialises parsed data (must be reachable by all executors).
SPARK_WORK_DIR = os.getenv("AUTOEDA_SPARK_WORK_DIR", (DATA_DIR / "spark_work").as_uri())
MAX_CONCURRENT_JOBS =_int("AUTOEDA_MAX_JOBS", 1)
# PDF tables: "auto" = geometry reader (pdfplumber) for ruled tables, Docling TableFormer as a
# second reader for borderless / unclear pages; "plumber" = geometry only; "docling" = Docling
# on every page that has text.
PDF_ENGINE = os.getenv("AUTOEDA_PDF_ENGINE", "auto")
DOCLING_MODELS = Path(os.getenv("AUTOEDA_DOCLING_MODELS", Path(__file__).resolve().parent.parent / "models" / "docling"))
DOCLING_MAX_PAGES = _int("AUTOEDA_DOCLING_MAX_PAGES", 60)   # per file: Docling takes seconds per page on CPU



def ensure_dirs():
    """Create working folders (not at import: Spark executors import this module too)."""
    for d in (UPLOAD_DIR, WORK_DIR, REPORT_DIR):
        d.mkdir(parents=True, exist_ok=True)
