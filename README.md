# AutoEDA — no-code automated EDA (PoC)

Drop in any data file and get a self-contained HTML report describing **every column**:
type and meaning, distribution, top values, formats, and data-quality issues. Made for
non-technical users:

* **English and Bahasa Indonesia** throughout the app and the report (instant switch; both
  languages are inside every downloaded report), with a **guided help tour** in each.
* **Inverted-pyramid report**: a verdict ("Usable after a few fixes"), the single most important problem
  and the key findings, each with *why it matters* and *what to do*, come first. The facts, the remaining
  checks and the per-column cards (problem columns first) follow. Technical background is at the bottom.
* A local **Ollama** model writes plain-language descriptions and a summary, and **reviews values for
  anomalies** rules can't catch: the same thing spelled differently (`Jkt` / `DKI Jakarta` → `Jakarta`),
  hidden "no data" values (`xxx`, `belum diisi`), values that don't belong in a column, names for
  columns without a header, and a second opinion on hard-to-repair rows. AI findings are checked against
  the real values and always labeled AI.

* **Formats**: CSV / TSV / TXT / DAT / fixed-width (also `.gz`), Excel `.xlsx .xlsm .xlsb .xls`,
  `.ods`, PDF (ruled and borderless tables, text layer, optional OCR), JSON / NDJSON,
  Parquet, ORC, Arrow / Feather, SQLite, XML.
* **Scale**: files up to ~100 GB on a 16 GB laptop. Parsing is parallel and profiling is
  out-of-core SQL on DuckDB with a bounded memory limit and spill to disk. A **Spark engine**
  is available for cluster deployments.
* **Messy data**: broken structure is repaired while reading, and every repair is logged
  in the report with examples (see the anomaly catalogue below).
* **Web UI + REST API + CLI**, Docker-ready. Running analyses can be **stopped** and **restarted** from the
  UI (or `POST /api/jobs/{id}/cancel` / `…/restart`); a stopped or failed job keeps its file so it can be
  run again.

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

**One command from your PC (Git Bash on Windows), over SSH/SCP.** The server needs only Docker and the compose plugin:

```bash
# first install: app image + Ollama image + AI models (reuses the models already on this PC)
OLLAMA_MODELS_DIR="$USERPROFILE/.ollama/models" scripts/deploy_airgap.sh user@server --dir /opt/autoeda

# later updates: only the new app image (~600 MB instead of several GB)
scripts/deploy_airgap.sh user@server --dir /opt/autoeda --app-only
```

It checks the server (Docker present, free disk), builds the bundle on this PC, copies it over
(`rsync` with resume when available, otherwise `scp`), verifies checksums on the server, loads the
images, preloads the models and starts the stack, then runs a health check. Options: `--port`,
`--key ~/.ssh/id_ed25519`, `--sudo` (docker needs sudo on the server), `--compose docker-compose.prod.yml`,
`--skip-build` (resend an existing bundle). Set up SSH key login first (`ssh-keygen`, then add the
`.pub` file to `~/.ssh/authorized_keys` on the server) to avoid repeated password prompts.

Manual alternative (USB or a transfer zone): `scripts/offline_bundle.sh offline_bundle`, copy the folder,
then on the server run `./install_offline.sh [docker-compose.prod.yml]`.

Spark: set `PYSPARK_VERSION` to the cluster's Spark version when building. Executors only need
Python 3.12. Simplest is to run the same image as the Spark workers (see the commented service in
`docker-compose.prod.yml`). The image trusts the root CAs in `certs/` (on this PoC laptop, Avast's);
remove them before building a production bundle if your policy requires it.

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
| `AUTOEDA_PDF_ENGINE` | `auto` | PDF tables: `auto` (see below), `plumber` (geometry only), `docling` (TableFormer on every page) |
| `AUTOEDA_DOCLING_MODELS` | `./models/docling` | Docling model folder (nothing is downloaded at runtime) |
| `AUTOEDA_DOCLING_MAX_PAGES` | `60` | per file; TableFormer takes seconds per page on CPU |

OCR for scanned PDFs is optional: `pip install pytesseract` and install the Tesseract binary.

### How PDF tables are read

Rules and AI work together; every value always comes from the PDF's own text layer.

1. **Ruled tables** (cell borders): the cell geometry from pdfplumber is exact. Merged cells are resolved
   from it: a cell merged down (a group value) is copied to every row it covers, a group heading merged
   across columns prefixes its sub-columns (`Sales / 2023`), and a cell merged across a whole body row
   (a section label) becomes a `section` column.
2. **Borderless tables**: cells are guessed from text alignment. With Docling installed, the
   **TableFormer** table-structure model (IBM Docling) reads the page too, and the better reading wins
   (consistent column types, no values glued together); the local model decides close calls.
3. **Still unclear** (no header on top, word fragments, glued values) and the Ollama model can read images
   (`qwen3.5` can): the page image goes to the **vision model once per table**. It returns column names and
   the first records; every word it returns is checked against the page text, then the column boundaries
   are measured from the real word positions and applied to all pages of that table.
4. **Tables over several pages** are stitched: columns are lined up by x-position, the header re-printed on
   each page is recognised by fuzzy matching (the local model decides borderline rows) and removed, and the
   rows repeated on every page tell how deep the header is, so a second header level (years, quarters) is
   never read as data. A page that starts a different table is kept apart.

Docling is optional (it adds PyTorch and ~670 MB of models):

```powershell
.venv\Scripts\pip install torch --index-url https://download.pytorch.org/whl/cpu
.venv\Scripts\pip install -r requirements-docling.txt
.venv\Scripts\docling-tools models download layout tableformer -o models/docling
```

Docker: `--build-arg WITH_DOCLING=1` (on by default in `docker-compose.yml` and the offline bundle;
`WITH_DOCLING=0 scripts/offline_bundle.sh` leaves it out).

## Anomaly catalogue

**Text / CSV structure.** The repair is chosen by how well the values fit each column's learned type:
encoding sniffing (UTF-8/16 with or without BOM, legacy code pages) · delimiter sniffing incl. Excel `sep=;`
· title / preamble lines · header missing, shorter or longer than the data · duplicate / blank header names
· unquoted delimiter inside a value (re-merged into the right column) · missing fields (inserted at the
best-fitting position) · merged fields (missing delimiter) · rows shifted by one column (sliding) ·
several records on one line · rows using another delimiter · trailing delimiters · repeated header rows
· blank / empty rows · `#` comment lines · unbalanced quotes · quoted multi-line values · backslash-escaped
quotes · NUL bytes · fixed-width / whitespace-aligned text.

**Excel.** Merged cells (a merged group value is copied to every row; a merged title, section label or note
stays one label) · title rows merged across the sheet · multi-row headers up to 3 levels ·
**several tables per sheet** (stacked or side by side) · empty spacer rows / columns · total / subtotal /
grand-total rows (incl. *Jumlah*) · section label rows (*Wilayah: Jawa*) turned into a `section`
column · footnotes / *Sumber:* / *Catatan* rows · formulas without saved results · hidden sheets ·
password-protected workbooks · very large sheets (streamed) · Excel serial-number dates · long IDs mangled to
scientific notation · wide / pivoted layouts (years as columns) · transposed tables · data truncated at
the Excel row limit · units / scale in headers (`Harga (Rp)`, *in thousands*).

**PDF.** Ruled and borderless tables · merged cells (vertical, horizontal, whole-row section labels) · tables
continuing over pages (repeated header removed even when read slightly differently, columns lined up when a
page is detected with a different number of columns) · header rows read as values · multi-level and wrapped
headers · records wrapped over several lines · phantom
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
scripts/deploy_airgap.sh           build + send + install over SSH/SCP in one command
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
    structure.py    shared layout rules + AI second opinion (header depth, repeated headers, vision layout)
    pdf.py          PDF tables: merged cells, page stitching, reader choice
    pdf_docling.py  optional Docling TableFormer reader
    excel.py others.py sink.py
  cancel.py         cooperative stop for running jobs (DuckDB / Spark / AI calls interrupted)
  profiling.py      DuckDB column profiler (type inference, stats, anomaly flags)
  spark_engine.py   Spark ingest + profiler (same output)
  llm.py            Ollama client, column descriptions, summary (EN / ID)
  llm_anomaly.py    AI anomaly review (variants, placeholders, misfits, names, ambiguous rows)
  i18n.py           English / Indonesian texts, advice per problem, locale number format
  narrative.py report.py templates/   plain-language narrative, bilingual report and web UI
```

## Known PoC limits

* The 2B model can misstate details. The deterministic "Profile summary" sits next to every AI text.
* Spark mode does not re-join quoted values spanning several lines.
* Multi-table detection runs on sheets up to 5M cells; larger sheets are treated as one table.
* Merged-column, case-variant and format checks run on a 100k-row sample.
