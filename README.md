# AutoEDA — no-code automated EDA (PoC)

Drop in any data file and get a self-contained HTML report describing **every column**:
type and meaning, distribution, top values, formats, and data-quality issues. A local
Ollama LLM adds plain-language descriptions and an executive summary.

* **Formats**: CSV / TSV / TXT / DAT / fixed-width (also `.gz`), Excel `.xlsx .xlsm .xlsb .xls`,
  `.ods`, PDF (ruled and borderless tables, text layer, optional OCR), JSON / NDJSON,
  Parquet, ORC, Arrow / Feather, SQLite, XML.
* **Scale**: files up to ~100 GB on a 16 GB laptop. Parsing is parallel and profiling is
  out-of-core SQL on DuckDB with a bounded memory limit and spill to disk. A **Spark engine**
  is available for cluster deployments.
* **Messy data**: broken structure is repaired while reading, and every repair is logged
  in the report with examples (see the anomaly catalogue below).
* **Web UI + REST API + CLI**, Docker-ready.

## Quick start (Windows / laptop, no Docker)

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
ollama pull qwen3.5:2b                      # optional: AI descriptions
.venv\Scripts\python -m uvicorn autoeda.web:app --port 8000
# open http://localhost:8000
```

CLI:

```powershell
.venv\Scripts\python -m autoeda.cli data.csv -o report.html            # with LLM
.venv\Scripts\python -m autoeda.cli big.csv --no-llm --max-rows 5000000  # quick look
```

## Docker (local PoC: app + Ollama + Spark standalone cluster)

```bash
docker compose up -d --build                     # autoeda, spark-master, spark-worker, ollama
docker compose exec ollama ollama pull qwen3.5:2b
```

| URL | |
|---|---|
| http://localhost:8000 | AutoEDA UI. Pick the engine per job: *Local (DuckDB)* or *Spark cluster* |
| http://localhost:8080 / :8081 | Spark master / worker UI |

* One image plays every role (`web`, `spark-master`, `spark-worker`), so Python, Spark and libraries are
  identical on driver and executors. Driver and workers share `./data` and `./input` at the same paths.
* Memory: about 10 GB in total. On Windows, give WSL2 enough RAM (`%UserProfile%\.wslconfig` → `[wsl2] memory=12GB`).
* App only: `docker compose up -d autoeda ollama`.
* Ollama already on the host: `OLLAMA_HOST=http://host.docker.internal:11434 docker compose up -d --no-deps autoeda spark-master spark-worker`.
* **HTTPS inspection** (corporate proxy, antivirus web shield such as Avast on the PoC laptop) breaks
  `pip` inside `docker build`. Put the inspecting root CA (public `.crt`, PEM) into `certs/` and it is
  trusted at build time and runtime. The PoC laptop's Avast root is already there; on other machines,
  delete it or replace it with your own.

## Air-gapped production

Nothing downloads at runtime. The report and UI embed all CSS/JS, DuckDB extensions are built in
(auto-install is disabled), the LLM is the local Ollama, and Spark executors need only the Python standard library.
Verified by running every supported format, plus the Spark engine, inside the container with `--network none`.

1. On a machine **with** internet (this laptop works: Git Bash + Docker Desktop):
   ```bash
   MODELS="qwen3.5:2b" PYSPARK_VERSION=3.5.3 scripts/offline_bundle.sh offline_bundle
   # reuse models already pulled on the host instead of downloading them again:
   OLLAMA_MODELS_DIR="$USERPROFILE/.ollama/models" scripts/offline_bundle.sh offline_bundle
   ```
   This produces `images.tar.gz` (app + Ollama images), `ollama_models.tgz`, the compose files, `install_offline.sh` and `SHA256SUMS`.
2. Copy the folder to the server (USB / transfer zone) and run `./install_offline.sh docker-compose.prod.yml`.
   It verifies checksums, `docker load`s the images, preloads the models into the `autoeda_ollama` volume and starts the stack with `--no-build`.
3. Spark: set `PYSPARK_VERSION` to the cluster's Spark version when building the bundle. Executors only need
   Python 3.12. Simplest is to run the same image as the Spark workers (see the commented service in `docker-compose.prod.yml`).

## Large files (tens of GB)

* Use **Server / cluster path** (files in `AUTOEDA_INPUT_DIR`) rather than browser upload.
  Upload works too: it is streamed straight to disk, but it doubles the disk use.
* Disk: intermediate Parquet takes roughly 15–30% of the CSV size, plus DuckDB spill space under `AUTOEDA_DATA_DIR`.
* Measured on the PoC laptop (i5-10th gen, 16 GB, Windows), 10M-row / 700 MB dirty CSV:
  * native path: **38 s, 0.57 GB peak RAM**. Extrapolated, 100 GB takes about 1.5 h and is I/O-bound.
  * full Python repair path (used for dirty files < 1 GB): 140 s, 0.64 GB peak RAM.
* `--max-rows` / the UI "Row limit" field profiles only the first N rows, for a fast first look.

How text files are parsed:

| Situation | Path |
|---|---|
| clean, or file > 1 GB | DuckDB native parallel reader → malformed lines it rejects **plus** rows whose values contradict their column type are re-repaired in Python |
| dirty and ≤ 1 GB | streaming Python repair of every line (catches everything, ~100k lines/s) |

## Engines

| | `local` (default) | `spark` |
|---|---|---|
| where | one machine, DuckDB | Spark cluster (standalone / YARN / k8s) |
| input | local files | local, `hdfs://`, `s3a://`, shared mounts |
| formats | all | delimited text, Parquet, ORC, JSON (others fall back to `local`) |
| repair | full | same RowRepairer, run on executors (`mapPartitions`); quoted values spanning lines are not re-joined |

Spark mode keeps type inference, format/pattern checks and merged-column detection on a
sample in DuckDB on the driver. All full-data statistics run as Spark SQL. The report is identical.

Production sketch (3 nodes × 72 cores / 786 GB): `docker-compose.prod.yml`. Point
`AUTOEDA_SPARK_MASTER` at the cluster and `AUTOEDA_SPARK_WORK_DIR` at storage every executor can reach.
Executors need the same Python minor version as the driver (standard library only); the `autoeda`
package is shipped to them automatically. A larger Ollama model (e.g. 14B–32B, included in the offline
bundle) gives noticeably more reliable narratives than the 2B PoC model.

## Configuration (environment variables)

| variable | default | meaning |
|---|---|---|
| `AUTOEDA_ENGINE` | `local` | `local` or `spark` |
| `AUTOEDA_MEMORY_LIMIT` | `4GB` | DuckDB working memory (spills beyond) |
| `AUTOEDA_THREADS` | `4` | DuckDB threads |
| `AUTOEDA_DATA_DIR` | `./data` | uploads, work files, spill, reports |
| `AUTOEDA_INPUT_DIR` | `./input` | server-side files allowed for "path" jobs |
| `AUTOEDA_SAMPLE_ROWS` | `100000` | rows used for type inference |
| `AUTOEDA_HUGE_ROWS` | `20000000` | above this: approximate quantiles / duplicates |
| `AUTOEDA_NATIVE_PARSE_MIN_BYTES` | `1 GiB` | dirty text files above this use the native reader |
| `AUTOEDA_MAX_JOBS` | `1` | concurrent jobs |
| `OLLAMA_HOST` / `OLLAMA_MODEL` | `http://localhost:11434` / `qwen3.5:2b` | LLM endpoint / model |
| `AUTOEDA_LLM_BATCH`, `AUTOEDA_LLM_MAX_COLUMNS`, `AUTOEDA_LLM_TIMEOUT` | 5, 80, 180 | LLM batching / limits |
| `AUTOEDA_SPARK_MASTER`, `AUTOEDA_SPARK_CONF`, `AUTOEDA_SPARK_WORK_DIR` | | Spark connection, `k=v;k=v` conf, shared work dir |
| `AUTOEDA_OCR_LANG` | `eng` | Tesseract language(s) for scanned PDFs, e.g. `ind+eng` |

OCR for scanned PDFs is optional: `pip install pytesseract` and install the Tesseract binary.

## Anomaly catalogue

**Text / CSV structure.** The repair is chosen by how well the values fit each column's learned type:
encoding sniffing (UTF-8/16 with or without BOM, legacy code pages) · delimiter sniffing incl. Excel `sep=;`
· title / preamble lines · header missing, shorter or longer than the data · duplicate / blank header names
· unquoted delimiter inside a value (re-merged into the right column) · missing fields (inserted at the
best-fitting position) · merged fields (missing delimiter) · rows shifted by one column (sliding) ·
several records on one line · rows using another delimiter · trailing delimiters · repeated header rows
· blank / empty rows · `#` comment lines · unbalanced quotes · quoted multi-line values · backslash-escaped
quotes · NUL bytes · fixed-width / whitespace-aligned text.

**Excel.** Merged cells (filled) · title rows merged across the sheet · multi-row headers up to 3 levels ·
**several tables per sheet** (stacked or side by side) · empty spacer rows / columns · total / subtotal /
grand-total rows (incl. *Jumlah*) · section label rows (*Wilayah: Jawa*) turned into a `section`
column · footnotes / *Sumber:* / *Catatan* rows · formulas without saved results · hidden sheets ·
password-protected workbooks · very large sheets (streamed) · Excel serial-number dates · long IDs mangled to
scientific notation · wide / pivoted layouts (years as columns) · transposed tables · data truncated at
the Excel row limit · units / scale in headers (`Harga (Rp)`, *in thousands*).

**PDF.** Ruled and borderless tables · merged cells · tables continuing over pages (repeated
header blocks removed) · multi-level and wrapped headers · records wrapped over several lines · phantom
empty columns · page headers / footers / page numbers · ligatures and full-width characters · text-only
PDFs · scanned PDFs (OCR) · encrypted PDFs.

**Values (all formats).** Placeholder nulls (`N/A`, `-`, `#DIV/0!`, `null`, `tidak ada` …) · numbers stored
as text · thousands / decimal conventions (`1,234.56` vs `1.234,56`), currency (`Rp`, `$`, `USD`),
percentages, accounting negatives `(1,234)`, trailing minus, footnote marks `12*` · values with units
(`62 kg`) · dates as text with format detection, ambiguous day/month order, future or pre-1900 dates · codes with
leading zeros · mixed types (with examples) · leading/trailing spaces · invisible characters (NBSP,
zero-width, control chars) · line breaks in cells · mojibake (`CafÃ©`) · case/spacing variants of the
same category · merged columns (`Jakarta - 12345`) · constant / empty columns · outliers, skew, sentinel
values (`999`, `-1`) · duplicate rows · strong correlations.

## Testing

```bash
python scripts/make_samples.py samples               # messy CSV + Excel
python scripts/make_samples.py samples --big 10000000
python scripts/make_anomaly_samples.py               # Excel / PDF / CSV anomaly corpus (PDF needs reportlab)
```

## Layout

```
Dockerfile docker/entrypoint.sh    one image: web | spark-master | spark-worker
docker-compose.yml                 local PoC stack (app, Ollama, Spark master + worker)
docker-compose.prod.yml            air-gapped production sketch
certs/                             extra root CAs for HTTPS-inspecting networks
scripts/offline_bundle.sh          build the air-gap bundle (internet side)
scripts/install_offline.sh         install from the bundle (air-gapped side)
autoeda/
  web.py            FastAPI app (streamed upload, path jobs, job queue, reports)
  cli.py            command line
  pipeline.py       ingest -> profile -> LLM -> report
  ingest/           format sniffing + readers
    delimited.py    text sniffing, native DuckDB parse + suspect re-check, streaming repair
    repair.py       RowRepairer: type-fit based structural repair
    grid.py         Excel/PDF layout normalisation (multi-table, headers, totals, sections ...)
    excel.py pdf.py others.py sink.py
  profiling.py      DuckDB column profiler (type inference, stats, anomaly flags)
  spark_engine.py   Spark ingest + profiler (same output)
  llm.py narrative.py report.py templates/
```

## Known PoC limits

* The 2B model can misstate details. The deterministic "Profile summary" sits next to every AI text.
* Spark mode does not re-join quoted values spanning several lines.
* Multi-table detection runs on sheets up to 5M cells; larger sheets are treated as one table.
* Merged-column, case-variant and format checks run on a 100k-row sample.
