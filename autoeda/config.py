"""Runtime configuration, all overridable through environment variables."""
import os
from pathlib import Path

# Snapshot of the process environment, kept by the settings module to tell where each value comes from
# (env var vs the Settings page settings.json vs the built-in default). Not used to *read* values; the
# module attributes below are the single source of truth.
OS_ENV = dict(os.environ)


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def _on(name: str, default: bool = True) -> bool:
    """Boolean from the environment: 0/false/no/off turn it off, anything else on."""
    return os.getenv(name, "1" if default else "0").lower() not in ("0", "false", "no", "off")


DATA_DIR = Path(os.getenv("AUTOEDA_DATA_DIR", "./data")).resolve()
UPLOAD_DIR = DATA_DIR / "uploads"
WORK_DIR = DATA_DIR / "work"
REPORT_DIR = DATA_DIR / "reports"
# Learned skills and the comments behind them: kept across restarts, in a folder that can be a git
# working copy (default: memory/ in the project, committed and synced with the code).
MEMORY_DIR = Path(os.getenv("AUTOEDA_MEMORY_DIR", Path(__file__).resolve().parent.parent / "memory")).resolve()
# Server-side folder users may profile in place (for files too large to upload).
INPUT_DIR = Path(os.getenv("AUTOEDA_INPUT_DIR", "./input")).resolve()

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3.5:2b")
# Threads per inference (llama.cpp): 0 = let Ollama decide. On a multi-socket CPU the default is one
# thread per logical core, which spreads over both sockets and runs slower than one socket's worth;
# set this to the physical cores of ONE socket (e.g. 18 on a Xeon Gold 6140). Sent as the Ollama
# model option num_thread with every request, which beats OLLAMA_NUM_THREADS for llama-server.
OLLAMA_NUM_THREAD: int = _int("OLLAMA_NUM_THREAD", 0)
# Keep the model resident between jobs (Ollama duration string: 4h, -1 = forever). Empty = Ollama default.
OLLAMA_KEEP_ALIVE: str = os.getenv("OLLAMA_KEEP_ALIVE", "")
# Thinking models: off = send think=false and use only the final answer (much faster, less output);
# on = let the model reason first. docker/Modelfile says the same in its system prompt, as a guard.
LLM_REASONING = _on("AUTOEDA_LLM_REASONING", False)
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
# An existing cluster: spark://host:7077 (standalone), yarn (needs HADOOP_CONF_DIR), or local[*].
SPARK_MASTER = os.getenv("AUTOEDA_SPARK_MASTER", "local[*]")
SPARK_CONF = os.getenv("AUTOEDA_SPARK_CONF", "")          # "key=value;key=value"
# Web UI / REST address the cluster's resources are read from (standalone master :8080, YARN
# ResourceManager :8088). Empty: derived from SPARK_MASTER for standalone.
SPARK_UI_URL = os.getenv("AUTOEDA_SPARK_UI", "")
# HDFS on the host: WebHDFS address (NameNode, e.g. http://namenode:9870) for browsing and for
# copying files Spark cannot read (Excel, PDF...) into the app; HDFS_ROOT is the hdfs:// prefix
# shown for picked files (e.g. hdfs://nameservice1).
HDFS_URL = os.getenv("AUTOEDA_HDFS_URL", "")
HDFS_USER = os.getenv("AUTOEDA_HDFS_USER", "")
HDFS_ROOT = os.getenv("AUTOEDA_HDFS_ROOT", "")
MAX_CONCURRENT_JOBS =_int("AUTOEDA_MAX_JOBS", 1)
# PDF tables: "auto" = geometry reader (pdfplumber) for ruled tables, Docling TableFormer as a
# second reader for borderless / unclear pages; "plumber" = geometry only; "docling" = Docling
# on every page that has text.
PDF_ENGINE = os.getenv("AUTOEDA_PDF_ENGINE", "auto")
DOCLING_MODELS = Path(os.getenv("AUTOEDA_DOCLING_MODELS", Path(__file__).resolve().parent.parent / "models" / "docling"))
DOCLING_MAX_PAGES = _int("AUTOEDA_DOCLING_MAX_PAGES", 60)   # per file: Docling takes seconds per page on CPU
OCR_LANG = os.getenv("AUTOEDA_OCR_LANG", "eng")


# EDA features (the Settings page switches them; settings.py)
EDA_DESCRIPTIVE = _on("AUTOEDA_EDA_DESCRIPTIVE")
EDA_UNIVARIATE = _on("AUTOEDA_EDA_UNIVARIATE")
EDA_MULTIVARIATE = _on("AUTOEDA_EDA_MULTIVARIATE")
EDA_QUALITY = _on("AUTOEDA_EDA_QUALITY")
EDA_DUPLICATES = _on("AUTOEDA_EDA_DUPLICATES")
AI_DEFAULT_ON = _on("AUTOEDA_AI_DEFAULT_ON")
AI_TABLE_LAYOUT = _on("AUTOEDA_AI_TABLE_LAYOUT")
AI_DESCRIPTIONS = _on("AUTOEDA_AI_DESCRIPTIONS")
AI_SUMMARY = _on("AUTOEDA_AI_SUMMARY")
AI_ANOMALY_REVIEW = _on("AUTOEDA_AI_ANOMALY_REVIEW")
MEMORY_ENABLED = _on("AUTOEDA_MEMORY")
KEEP_UPLOADS = _on("AUTOEDA_KEEP_UPLOADS")      # keep an uploaded file after its report (for 'Run again')

# Sign-in (auth.py). Off only for a single-user laptop: then everyone is the administrator.
# Not on the Settings page on purpose: switching it off must need access to the server.
AUTH_ENABLED = _on("AUTOEDA_AUTH")
ADMIN_USER = os.getenv("AUTOEDA_ADMIN_USER", "admin")
ADMIN_PASSWORD = os.getenv("AUTOEDA_ADMIN_PASSWORD", "")   # first start only; empty = a random one, logged
SESSION_HOURS = _int("AUTOEDA_SESSION_HOURS", 12)



def ensure_dirs():
    """Create working folders (not at import: Spark executors import this module too)."""
    for d in (UPLOAD_DIR, WORK_DIR, REPORT_DIR):
        d.mkdir(parents=True, exist_ok=True)
