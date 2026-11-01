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
  UI (or `POST /api/jobs/{id}/cancel` / `…/restart`); every job keeps its file until its report is deleted,
  so any report in the list can be run again (e.g. after an update of the app). *Delete file, keep report*
  frees the space of one upload (`POST /api/jobs/{id}/drop-file`); the setting *Keep uploaded files* off
  deletes every upload as soon as its report is ready. Files in the server's input folder are never deleted.
* **Settings page** (`/settings`): switch EDA features on or off (descriptive statistics, univariate,
  multivariate, data-quality checks, duplicate rows), each AI step, memory, PDF reader, model and
  performance limits. Saved in `data/settings.json`, so they survive restarts; they override the
  environment variables of the same name and apply to the next analysis (`GET|PUT|DELETE /api/settings`).
* **Sign-in** with users and roles. A *user* sees only their own reports and files; an *administrator*
  sees everything and alone may change Settings, Memory and Users. See [Sign-in](#sign-in).
* **Files page** (`/files`): every uploaded file with its size, report size, owner and date; disk space
  used and free; sort, search, download the original, delete a file (keep the report) or both, also
  several at once. Administrators also see and free left-over upload folders no report refers to.
* **Report**: Bulma dark theme (light switch ◐), each table in its own framed block (*Table 2 of 4*, pages /
  sheet, rows, columns), a column-statistics table where **clicking a row opens the column's card**
  (counts, chart, common values and every *thing to check* with why it matters and what to do; ← → to
  step through columns). **Export PDF** opens the print window (choose *Save as PDF*): light colours, every
  column card and detail included, one table per page. The comment box (*Were the tables read
  correctly?*) is at the bottom, with a table picker.

## Sign-in

On the first start, the app creates the administrator `AUTOEDA_ADMIN_USER` (default `admin`) with the
password `AUTOEDA_ADMIN_PASSWORD`; when that is not set, a random password is printed to the log
(`docker compose logs autoeda`). Sign in, then change it under 👤 → *Account & password*. Add users on the
**Users** page (`/users`), or on the server:

```bash
python -m autoeda.auth list | add NAME [--admin] | passwd NAME | delete NAME
# in Docker: docker compose exec autoeda python -m autoeda.auth passwd admin
```

* Users and salted PBKDF2 password hashes live in `data/users.json`; sessions are signed cookies
  (`data/secret.key`, HttpOnly, SameSite=Lax, Secure over HTTPS) valid `AUTOEDA_SESSION_HOURS` (12).
  Changing a password signs that user out everywhere else. Five wrong passwords in 5 minutes block that
  name from that address for a while.
* Scripts use HTTP Basic auth on the REST API: `curl -u ann:secret http://host:8000/api/jobs`.
* Reports made before sign-in existed have no owner; administrators see them.
* `AUTOEDA_AUTH=0` switches sign-in off (single-user laptop): everyone is the administrator. It is not
  on the Settings page on purpose.

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
# first install: app image + Ollama image + AI models, taken from this PC's Ollama model folder
# (missing models are pulled there first; this PC uses "D:\ollama models", the Ollama default is ~/.ollama/models)
OLLAMA_MODELS_DIR="/d/ollama models" scripts/deploy_airgap.sh user@server --dir /opt/autoeda \
  --compose docker-compose.prod.yml

# later updates: only the new app image (~600 MB instead of several GB)
scripts/deploy_airgap.sh user@server --dir /opt/autoeda --app-only
```

It checks the server (Docker present, free disk), builds the bundle on this PC, copies it over
(`rsync` with resume when available, otherwise `scp`), verifies checksums on the server, loads the
images, preloads the models and starts the stack, then runs a health check. Options: `--port`,
`--key ~/.ssh/id_ed25519`, `--sudo` (docker needs sudo on the server), `--compose docker-compose.prod.yml`,
`--skip-build` (resend an existing bundle). Set up SSH key login first (`ssh-keygen`, then add the
`.pub` file to `~/.ssh/authorized_keys` on the server) to avoid repeated password prompts.

**AI model for the production servers** (3 × 72 CPU cores, 786 GB RAM, no GPU). On CPU, speed depends on
the parameters used per token, not on RAM, so the bundle includes:

| model | size | why |
|---|---|---|
| `qwen3.5:35b-a3b` (production default) | 22 GB | mixture-of-experts, 35B total / ~3B active: roughly the speed of a 3–4B model, far more reliable descriptions, anomaly review and layout decisions; reads images (borderless PDF tables) |
| `qwen3.5:2b` | 2.7 GB | fast fallback, selectable under *Advanced settings* |

Not chosen: `qwen3.5:27b` (dense, every parameter used per token: about 9× slower on CPU) and
`qwen3.5:122b-a10b` (its Ollama build has no image input, ~3× slower). Change the set with
`MODELS="…" scripts/offline_bundle.sh` and the default with `OLLAMA_MODEL` in `docker-compose.prod.yml`.
The bundle contains only the listed models, not everything in the model folder.

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

### An existing Spark cluster and HDFS on the host

`docker-compose.prod.yml` builds nothing and starts no Spark master or worker: the app connects to the
cluster that already runs on the host as a Spark **client** (driver). Only the Spark client libraries
(PySpark + Java) are in the image; build it with the cluster's version
(`spark-submit --version` → `--build-arg WITH_SPARK=1 --build-arg PYSPARK_VERSION=<that version>`).

* **Settings → Spark cluster and HDFS** sets the master (`spark://host:7077`, `yarn`, `local[*]`), the
  `key=value;…` configuration, the work folder and the cluster's web UI address. A change applies to the
  next Spark job, no restart. The page shows what the cluster has now, read from the standalone master's
  `/json` or YARN's `/ws/v1/cluster/metrics`: alive workers, cores and memory used / total, running apps,
  each worker, and the app's Spark client version (`GET /api/spark`). Size `spark.executor.*` and
  `spark.cores.max` to those numbers, leaving room for the cluster's other users.
* **HDFS files are analysed in place.** Mount the host's Hadoop client configuration
  (`/etc/hadoop/conf`, `HADOOP_CONF_DIR`) so `hdfs://nameservice1/...` resolves, and set the WebHDFS address
  (`AUTOEDA_HDFS_URL`, NameNode HTTP port 9870, or HttpFS 14000). Then *Advanced settings → Browse HDFS*
  on the start page lists folders and files (`GET /api/hdfs?path=/`), or type an `hdfs://` path:
  * Spark engine + CSV / TSV / Parquet / ORC / JSON: the executors read the file on the cluster; nothing
    is copied.
  * other formats (Excel, PDF, XML, …) or the local engine: the file is streamed into the app's work
    folder over WebHDFS for the run and removed afterwards.
* Kerberos: give Spark a principal and keytab in the configuration (`spark.kerberos.principal`,
  `spark.kerberos.keytab`) and mount `/etc/krb5.conf` and the keytab; WebHDFS browsing then needs a
  gateway that accepts the app's credentials (HttpFS / Knox).

Production sketch (3 nodes × 72 cores / 786 GB): `docker-compose.prod.yml`. Point
`AUTOEDA_SPARK_MASTER` at the cluster and `AUTOEDA_SPARK_WORK_DIR` at storage every executor can reach.
Executors need the same Python minor version as the driver (standard library only); the `autoeda`
package is shipped to them automatically. The production bundle uses `qwen3.5:35b-a3b` (see Air-gapped
production), noticeably more reliable than the 2B PoC model.

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
| `AUTOEDA_SPARK_MASTER`, `AUTOEDA_SPARK_CONF`, `AUTOEDA_SPARK_WORK_DIR` | `local[*]` | Spark connection, `k=v;k=v` conf, shared work dir (also on the Settings page) |
| `AUTOEDA_SPARK_UI` | from the master | cluster web UI / REST address the resources are read from (YARN: ResourceManager `:8088`) |
| `AUTOEDA_HDFS_URL`, `AUTOEDA_HDFS_ROOT`, `AUTOEDA_HDFS_USER` | | WebHDFS address, `hdfs://` prefix (fs.defaultFS), `user.name` |
| `AUTOEDA_AUTH` | `1` | sign-in on (`0`: off, everyone is administrator) |
| `AUTOEDA_ADMIN_USER`, `AUTOEDA_ADMIN_PASSWORD` | `admin`, random | first administrator, created at the first start only |
| `AUTOEDA_SESSION_HOURS` | `12` | how long a sign-in lasts |
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
   (a section label) becomes a `section` column. A table ruled between columns but not between rows
   (several records stacked in one cell) is split back into one row per record; a frame drawn round
   another table, and lines that are only row shading, are not taken as cell borders.
2. **Borderless tables**: lines that read as table rows (several short phrases, or a label and numbers)
   are grouped, and columns are cut only at whitespace gutters shared by those lines, so a word is never
   split and running text (articles, letters) is not mistaken for a table. With Docling installed, the
   **TableFormer** table-structure model (IBM Docling) reads the page too, and the better reading wins
   (consistent column types, no values glued together); the local model decides close calls.
3. **Still unclear** (no header on top, word fragments, glued values) and the Ollama model can read images
   (`qwen3.5` can): the page image goes to the **vision model once per table**. It returns column names and
   the first records; every word it returns is checked against the page text, then the column boundaries
   are measured from the real word positions and applied to all pages of that table.
4. **Tables over several pages** are stitched: columns are lined up by x-position, the header re-printed on
   each page is recognised by fuzzy matching (the local model decides borderline rows) and removed, and the
   rows repeated on every page tell how deep the header is, so a second header level (years, quarters) is
   never read as data. A page that starts a different table (a new title above it, e.g. "LAMPIRAN II")
   is kept apart; a "(Lanjutan) / (continued)" note keeps it joined. A wide table continued to the right on
   the next page (same row labels, other columns, as in BPS *Dalam Angka* books) is joined side by side.
5. **Clean-up**: page numbers and running headers, "(1) (2) (3)" column-numbering rows, signature boxes,
   diagonal watermarks, glyphs printed twice for bold, and letter-spaced values (`0 3 / 2 5 / 2 0 16`).
   A PDF with running text only stops with "No table found" instead of profiling words as columns.

Docling is optional (it adds PyTorch and ~670 MB of models):

```powershell
.venv\Scripts\pip install torch --index-url https://download.pytorch.org/whl/cpu
.venv\Scripts\pip install -r requirements-docling.txt
.venv\Scripts\docling-tools models download layout tableformer -o models/docling
```

Docker: `--build-arg WITH_DOCLING=1` (on by default in `docker-compose.yml` and the offline bundle;
`WITH_DOCLING=0 scripts/offline_bundle.sh` leaves it out).

## Memory: learning from comments

Under every table of a report (when it is opened from the app) there is a comment box: *"Was this table
read correctly?"*. Users write what is wrong in their own words, in English or Indonesian, for example:

* "table 2 and 3 are one table" / "tabel 2 dan 3 adalah satu tabel"
* "the header has 2 rows" / "judul kolom ada 2 baris"
* "NIP is an ID, not a number" / "Kolom Desa adalah kategori"
* "'Kecamatan Manggelewa' adalah total, bukan data" / "remove rows containing 'Ditandatangani'"
* "rename column 'KEHADIRAN' to 'Attendance'"

Each comment becomes a **skill**: an instruction for the local model plus structured hints the reader obeys
(`header_rows`, `join_tables`, `drop_rows`, `column_types`, `rename_columns`), and keywords (publisher,
document type, column names) that tell which later files it applies to. The local model writes the skill in
general terms; explicit numbers and names are read by rules, and a hint the model proposes is kept only if
the comment talks about it. Words that name the document's own subject (the district in "Kecamatan
Manggelewa") become wildcards, so the lesson carries over to the next district's book. A comment on a kind
of file that already has a skill refines that skill instead of adding a copy.

Skills apply to new runs at once: the report lists the skills it used, and *Re-run with my feedback* reads
the file again. The **Memory** page (`/memory`, linked from the main page) shows every skill and comment;
any skill can be edited, switched off or deleted there, and *Clear memory* forgets everything.

Memory is persistent: `memory/skills.json` and `memory/feedback.jsonl` in the project folder (or
`AUTOEDA_MEMORY_DIR`; Docker mounts it at `/memory`). Commit the folder to sync what was learned between
machines with git; per-server usage counts stay in `data/memory_usage.json` so they never cause merge
conflicts. API: `POST /api/jobs/{id}/feedback`, `GET /api/memory`,
`PUT|DELETE /api/memory/skills/{id}`.

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
pip install -r requirements-dev.txt
python -m pytest tests -q                            # real-world PDF / Excel / CSV corpus + unit tests
AUTOEDA_TEST_DOCLING=1 python -m pytest tests -q     # the PDF corpus with the Docling table model
```

`tests/data/tabular` holds Excel and CSV exports of Indonesian open-data portals (titles, sub-rows,
merged headers, `#REF!`, '.' as thousands separator). `tests/data/pdf` holds real documents (government statistics, budgets, election and procurement records,
Indonesian BPS / Kemenkes / CPNS publications; sources in `SOURCES.md`). Each case in
`tests/test_pdf_corpus.py` states the correct extraction; known shortcomings are strict `xfail` cases, so
a fix shows up as XPASS.

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
  web.py            FastAPI app (sign-in, streamed upload, path / HDFS jobs, job queue, files, reports)
  auth.py           users, password hashes, signed sessions, `python -m autoeda.auth`
  hdfs.py           WebHDFS: browse HDFS, copy files Spark cannot read into the app
  bulma.py          vendored Bulma CSS (static/), cut down to the rules each report uses
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
  static/           bulma.min.css (v1.0.4, MIT), app.css, app.js: shared by the app pages
```

## Known PoC limits

* The 2B model can misstate details. The deterministic "Profile summary" sits next to every AI text.
* Spark mode does not re-join quoted values spanning several lines.
* Multi-table detection runs on sheets up to 5M cells; larger sheets are treated as one table.
* Merged-column, case-variant and format checks run on a 100k-row sample.
