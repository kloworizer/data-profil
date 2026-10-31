"""English / Bahasa Indonesia texts for the report.

Messages produced by the engine are {"tpl": English template, "p": values}. They
are rendered with `render(m, lang)`: the Indonesian template is looked up in
ID_TEMPLATES by its English template (falling back to English), and numbers are
formatted per locale (1,234.5 vs 1.234,5)."""
from __future__ import annotations

import string

LANGS = ("en", "id")


class _LocaleFormatter(string.Formatter):
    def __init__(self, lang: str):
        self.lang = lang

    def format_field(self, value, spec):
        if isinstance(value, str) and self.lang == "id":
            value = VALUE_ID.get(value, value)
        if not spec and isinstance(value, (int, float)) and not isinstance(value, bool):
            return fmt_num(value, self.lang)
        s = format(value, spec)
        if self.lang == "id" and isinstance(value, (int, float)) and not isinstance(value, bool):
            s = s.translate(str.maketrans({",": ".", ".": ","}))
        return s


def render(m, lang: str = "en") -> str:
    if m is None:
        return ""
    if isinstance(m, str):
        return m
    tpl = m.get("tpl", "")
    if lang == "id":
        tpl = ID_TEMPLATES.get(tpl, tpl)
    try:
        return _LocaleFormatter(lang).format(tpl, **(m.get("p") or {}))
    except (KeyError, IndexError, ValueError):
        return m.get("text", tpl)


def fmt_num(v, lang: str = "en") -> str:
    """Human number: thousands separators, few decimals."""
    if v is None:
        return "–"
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, float):
        if v != v:
            return "–"
        a = abs(v)
        if a != 0 and (a >= 1e12 or a < 1e-3):
            s = f"{v:.3g}"
        elif v.is_integer():
            s = f"{int(v):,}"
        else:
            s = f"{v:,.{2 if a >= 100 else 3}f}".rstrip("0").rstrip(".")
    elif isinstance(v, int):
        s = f"{v:,}"
    else:
        return str(v)
    if lang == "id":
        s = s.translate(str.maketrans({",": ".", ".": ","}))
    return s


# --- values used inside messages ------------------------------------------------------
VALUE_ID = {"Sun": "Minggu", "Mon": "Senin", "Tue": "Selasa", "Wed": "Rabu", "Thu": "Kamis", "Fri": "Jumat",
            "Sat": "Sabtu", "true": "ya", "false": "tidak", "year": "tahun", "month": "bulan", "day": "hari"}

# --- column types / meanings ----------------------------------------------------------
TYPE_LABEL = {
    "en": {"integer": "Whole number", "float": "Decimal number", "categorical": "Category", "text": "Text",
           "date": "Date", "datetime": "Date & time", "boolean": "Yes / No", "empty": "Empty"},
    "id": {"integer": "Bilangan bulat", "float": "Bilangan desimal", "categorical": "Kategori", "text": "Teks",
           "date": "Tanggal", "datetime": "Tanggal & waktu", "boolean": "Ya / Tidak", "empty": "Kosong"},
}
SEMANTIC_LABEL = {
    "en": {"email": "email address", "url": "web address", "uuid": "unique code", "ip_address": "IP address",
           "phone": "phone number", "json": "structured text (JSON)", "identifier": "ID / unique code",
           "free text": "free text", "code": "code", "binary flag": "0/1 flag", "discrete / code": "small set of numbers"},
    "id": {"email": "alamat email", "url": "alamat web", "uuid": "kode unik", "ip_address": "alamat IP",
           "phone": "nomor telepon", "json": "teks terstruktur (JSON)", "identifier": "ID / kode unik",
           "free text": "teks bebas", "code": "kode", "binary flag": "penanda 0/1", "discrete / code": "sedikit pilihan angka"},
}

# --- flags: short title, why it matters, what to do --------------------------------------
FLAG_INFO = {
    "empty": ("Empty column", "Kolom kosong",
              "The column has no data at all, so it cannot be used.", "Kolom ini tidak berisi data sama sekali.",
              "Remove it, or check whether the data was lost when the file was exported.",
              "Hapus kolom ini, atau periksa apakah datanya hilang saat file diekspor."),
    "missing": ("Empty values", "Nilai kosong",
                "Empty cells make totals, averages and counts less reliable.",
                "Sel kosong membuat total, rata-rata, dan hitungan kurang akurat.",
                "Find out why values are missing; fill them in from the source if possible, or exclude those rows.",
                "Cari tahu mengapa nilainya kosong; lengkapi dari sumber jika bisa, atau keluarkan baris tersebut."),
    "null_tokens": ("Placeholder values", "Nilai pengganti",
                    "Texts like 'N/A' or '-' mean 'no data' and were counted as empty.",
                    "Teks seperti 'N/A' atau '-' berarti 'tidak ada data' dan dihitung sebagai kosong.",
                    "Use one consistent way to mark missing data (an empty cell is best).",
                    "Gunakan satu cara yang sama untuk menandai data kosong (sel kosong paling baik)."),
    "constant": ("Same value everywhere", "Nilai selalu sama",
                 "A column that never changes adds no information.", "Kolom yang tidak pernah berubah tidak menambah informasi.",
                 "You can usually ignore or remove it.", "Biasanya kolom ini bisa diabaikan atau dihapus."),
    "whitespace": ("Extra spaces", "Spasi berlebih",
                   "'Jakarta' and 'Jakarta ' look the same but are counted as different values.",
                   "'Jakarta' dan 'Jakarta ' terlihat sama tetapi dihitung sebagai nilai berbeda.",
                   "Trim spaces (Excel: TRIM function).", "Hapus spasi berlebih (Excel: fungsi TRIM)."),
    "imbalance": ("One value dominates", "Satu nilai mendominasi",
                  "When almost every row has the same value, the column tells little.",
                  "Jika hampir semua baris bernilai sama, kolom ini kurang informatif.",
                  "Check whether this is expected, or whether a default value was filled in automatically.",
                  "Periksa apakah ini wajar, atau apakah nilai bawaan terisi otomatis."),
    "invisible_chars": ("Invisible characters", "Karakter tak terlihat",
                        "Hidden characters (often from copy-paste or web pages) make values fail to match and numbers fail to calculate.",
                        "Karakter tersembunyi (sering dari salin-tempel atau halaman web) membuat nilai tidak cocok dan angka gagal dihitung.",
                        "Clean them (Excel: CLEAN and SUBSTITUTE(A1,CHAR(160),\" \")).",
                        "Bersihkan (Excel: CLEAN dan SUBSTITUTE(A1,CHAR(160),\" \"))."),
    "line_breaks": ("Line breaks in cells", "Baris baru di dalam sel",
                    "Line breaks inside cells can break exports to CSV and other systems.",
                    "Baris baru di dalam sel dapat merusak ekspor ke CSV dan sistem lain.",
                    "Replace them with a space or a separator if they are not needed.",
                    "Ganti dengan spasi atau pemisah jika tidak diperlukan."),
    "mojibake": ("Garbled characters", "Karakter rusak",
                 "The file was saved or opened with the wrong character set, so letters like é became Ã©.",
                 "File disimpan atau dibuka dengan set karakter yang salah, sehingga huruf seperti é menjadi Ã©.",
                 "Export the data again as UTF-8.", "Ekspor ulang data dengan format UTF-8."),
    "scientific_notation": ("Numbers turned into 1.2E+15", "Angka berubah menjadi 1,2E+15",
                            "Long numbers (IDs, phone, account numbers) were shortened by Excel; the last digits may be lost for good.",
                            "Angka panjang (ID, telepon, nomor rekening) dipendekkan oleh Excel; digit terakhir bisa hilang permanen.",
                            "Get the data again from the source and format the column as Text before opening it in Excel.",
                            "Ambil ulang data dari sumber dan atur kolom sebagai Teks sebelum dibuka di Excel."),
    "ambiguous_dates": ("Day/month unclear", "Hari/bulan tidak jelas",
                        "A date like 03/04/2024 can mean 3 April or 4 March.",
                        "Tanggal seperti 03/04/2024 bisa berarti 3 April atau 4 Maret.",
                        "Confirm the date format with whoever produced the file; prefer the YYYY-MM-DD format.",
                        "Pastikan format tanggal dengan pembuat file; sebaiknya gunakan format YYYY-MM-DD."),
    "mixed_type": ("Mixed types", "Tipe campuran",
                   "Some values do not match the rest (e.g. text in a number column), so they cannot be calculated.",
                   "Sebagian nilai tidak sesuai dengan yang lain (mis. teks di kolom angka), sehingga tidak bisa dihitung.",
                   "Look at the examples and correct or remove those values.",
                   "Lihat contohnya lalu perbaiki atau hapus nilai tersebut."),
    "outliers": ("Unusual values", "Nilai tidak biasa",
                 "A few values are far from the rest. They may be real (big orders) or mistakes (typing errors).",
                 "Beberapa nilai jauh dari yang lain. Bisa nyata (pesanan besar) atau salah ketik.",
                 "Check the largest and smallest values against the source.",
                 "Periksa nilai terbesar dan terkecil terhadap sumbernya."),
    "skew": ("Uneven spread", "Sebaran tidak merata",
             "The average is pulled by a few extreme values; the middle value (median) describes it better.",
             "Rata-rata tertarik oleh beberapa nilai ekstrem; nilai tengah (median) lebih mewakili.",
             "Use the median rather than the average when summarising this column.",
             "Gunakan median, bukan rata-rata, saat meringkas kolom ini."),
    "rare_negatives": ("Unexpected negatives", "Nilai negatif tak terduga",
                       "A few negative values in a column that is otherwise positive are often mistakes or refunds.",
                       "Sedikit nilai negatif di kolom yang biasanya positif sering merupakan kesalahan atau pengembalian.",
                       "Check whether these negative values are correct.", "Periksa apakah nilai negatif ini benar."),
    "excel_serial_dates": ("Dates shown as numbers", "Tanggal tampil sebagai angka",
                           "Excel stores dates as day counts; here they appear as plain numbers like 45123.",
                           "Excel menyimpan tanggal sebagai hitungan hari; di sini tampil sebagai angka seperti 45123.",
                           "In Excel, format the column as Date.", "Di Excel, ubah format kolom menjadi Tanggal."),
    "sentinel": ("Possible fake value", "Kemungkinan nilai palsu",
                 "Values like 999 or -1 are often used to mean 'unknown' and distort averages.",
                 "Nilai seperti 999 atau -1 sering dipakai untuk 'tidak diketahui' dan merusak rata-rata.",
                 "Replace them with empty cells if they mean 'unknown'.",
                 "Ganti dengan sel kosong jika artinya 'tidak diketahui'."),
    "future_dates": ("Dates in the future", "Tanggal di masa depan",
                     "Future dates are fine for plans or due dates, but are mistakes for past events.",
                     "Tanggal masa depan wajar untuk rencana atau jatuh tempo, tetapi salah untuk kejadian lampau.",
                     "Check whether future dates make sense here.", "Periksa apakah tanggal masa depan masuk akal di sini."),
    "old_dates": ("Very old dates", "Tanggal sangat lama",
                  "Dates before 1900 are usually placeholders (like 1900-01-01) rather than real dates.",
                  "Tanggal sebelum 1900 biasanya nilai pengganti (seperti 1900-01-01), bukan tanggal sebenarnya.",
                  "Replace placeholder dates with empty cells.", "Ganti tanggal pengganti dengan sel kosong."),
    "case_variants": ("Same value, different spelling", "Nilai sama, penulisan beda",
                      "'Jakarta', 'jakarta' and 'JAKARTA ' are counted as different categories.",
                      "'Jakarta', 'jakarta', dan 'JAKARTA ' dihitung sebagai kategori berbeda.",
                      "Standardise the spelling (Excel: PROPER/UPPER and TRIM).",
                      "Seragamkan penulisan (Excel: PROPER/UPPER dan TRIM)."),
    "merged_column": ("Several values in one cell", "Beberapa nilai dalam satu sel",
                      "One cell holds more than one piece of information (e.g. city and postcode), which makes filtering hard.",
                      "Satu sel berisi lebih dari satu informasi (mis. kota dan kode pos), sehingga sulit difilter.",
                      "Split the column (Excel: Data > Text to Columns).",
                      "Pisahkan kolom (Excel: Data > Text to Columns)."),
    "row_limit": ("Data may be cut off", "Data mungkin terpotong",
                  "The row count equals Excel's maximum, so rows beyond it were probably lost.",
                  "Jumlah baris sama dengan batas maksimum Excel, jadi baris selebihnya mungkin hilang.",
                  "Export the data again as CSV directly from the source system.",
                  "Ekspor ulang data sebagai CSV langsung dari sistem sumber."),
    "wide_format": ("Wide (pivot) layout", "Tata letak melebar (pivot)",
                    "Periods are spread across columns, which is fine for reading but hard to analyse.",
                    "Periode tersebar di banyak kolom; mudah dibaca, tetapi sulit dianalisis.",
                    "For analysis, turn it into a long table: one row per item and period.",
                    "Untuk analisis, ubah menjadi tabel memanjang: satu baris per item dan periode."),
    "transposed": ("Table may be sideways", "Tabel mungkin terbalik",
                   "Records seem to be stored in columns instead of rows.",
                   "Data tampaknya disimpan per kolom, bukan per baris.",
                   "Transpose the table (Excel: Paste Special > Transpose).",
                   "Balik tabel (Excel: Paste Special > Transpose)."),
    "ai_variants": ("Same meaning, different spelling (AI)", "Arti sama, penulisan beda (AI)",
                    "The AI found values that probably mean the same thing, which splits your counts.",
                    "AI menemukan nilai yang kemungkinan berarti sama, sehingga hitungan terpecah.",
                    "Replace the variants with the suggested standard value.",
                    "Ganti variasi tersebut dengan nilai standar yang disarankan."),
    "ai_placeholders": ("Hidden 'no data' values (AI)", "Nilai 'tanpa data' tersembunyi (AI)",
                        "Some values look like stand-ins for missing data.",
                        "Beberapa nilai tampak seperti pengganti data kosong.",
                        "Replace them with empty cells.", "Ganti dengan sel kosong."),
    "ai_suspicious": ("Values that don't fit (AI)", "Nilai yang tidak cocok (AI)",
                      "The AI found values that do not seem to belong in this column.",
                      "AI menemukan nilai yang tampaknya tidak cocok untuk kolom ini.",
                      "Check these values against the source.", "Periksa nilai ini terhadap sumbernya."),
}


def flag_info(code: str, lang: str) -> dict:
    t = FLAG_INFO.get(code)
    if not t:
        return {"title": code.replace("_", " "), "why": "", "todo": ""}
    i = 0 if lang == "en" else 1
    return {"title": t[i], "why": t[2 + i], "todo": t[4 + i]}


# --- structural repairs --------------------------------------------------------------
REPAIR_LABEL = {
    "preamble_skipped": ("Title or note lines above the table were skipped", "Baris judul/catatan di atas tabel dilewati"),
    "blank_line": ("Blank lines skipped", "Baris kosong dilewati"),
    "empty_row": ("Empty rows skipped", "Baris tanpa isi dilewati"),
    "repeated_header": ("Repeated header rows removed", "Baris judul kolom yang berulang dihapus"),
    "trailing_delimiter": ("Extra separators at line ends removed", "Pemisah berlebih di akhir baris dihapus"),
    "unquoted_delimiter_merged": ("A value contained the separator (e.g. a comma) and was put back together",
                                  "Nilai yang mengandung pemisah (mis. koma) disatukan kembali"),
    "overflow_joined": ("Rows with too many values: extras joined into the last column",
                        "Baris dengan nilai berlebih: kelebihannya digabung ke kolom terakhir"),
    "merged_rows_split": ("Several records stuck on one line were split", "Beberapa data dalam satu baris dipisahkan"),
    "mixed_delimiter": ("Rows using a different separator were re-read", "Baris dengan pemisah berbeda dibaca ulang"),
    "merged_fields_split": ("Values stuck together were split apart", "Nilai yang menempel dipisahkan"),
    "missing_fields_padded": ("Rows with missing values were aligned to the right columns",
                              "Baris dengan nilai hilang disejajarkan ke kolom yang benar"),
    "sliding_row": ("Rows shifted by one column were moved back", "Baris yang bergeser satu kolom dikembalikan"),
    "unbalanced_quote": ("Broken quotation marks handled", "Tanda kutip rusak ditangani"),
    "header_padded": ("Missing column names were created", "Nama kolom yang hilang dibuatkan"),
    "header_truncated": ("Extra column names without data were dropped", "Nama kolom berlebih tanpa data dihapus"),
    "multirow_header_combined": ("Headers spread over several rows were combined", "Judul kolom bertingkat digabungkan"),
    "merged_cells_filled": ("Merged cells were filled in", "Sel gabungan (merge) diisi"),
    "encoding_fallback": ("Special character encoding was converted", "Pengodean karakter dikonversi"),
    "empty_column_dropped": ("Empty spacer columns removed", "Kolom kosong pengisi dihapus"),
    "total_row_removed": ("Total / subtotal rows removed (they would double count)",
                          "Baris total / subtotal dihapus (agar tidak terhitung dua kali)"),
    "note_row_skipped": ("Footnote / note rows skipped", "Baris catatan kaki dilewati"),
    "section_row_to_column": ("Group label rows turned into a 'section' column",
                              "Baris label kelompok diubah menjadi kolom 'section'"),
    "wrapped_row_merged": ("Records split over several lines were joined", "Data yang terpecah ke beberapa baris digabung"),
    "uncached_formulas": ("Formulas without saved results (values missing)",
                          "Rumus tanpa hasil tersimpan (nilainya tidak ada)"),
    "page_furniture_removed": ("Page headers, footers and page numbers removed",
                               "Kepala/kaki halaman dan nomor halaman dihapus"),
    "comment_line": ("Comment lines skipped", "Baris komentar dilewati"),
    "nul_bytes_removed": ("Invalid null characters removed", "Karakter null tidak valid dihapus"),
    "ocr_page": ("Scanned pages read with text recognition (OCR)", "Halaman hasil pindai dibaca dengan OCR"),
    "columns_realigned": ("Pages where the columns were detected differently were lined up by position",
                          "Halaman dengan kolom yang terbaca berbeda disejajarkan menurut posisinya"),
    "ai_layout": ("AI helped decide the table layout (header rows, page breaks)",
                  "AI membantu menentukan tata letak tabel (baris judul kolom, pergantian halaman)"),
}


def repair_label(kind: str, lang: str) -> str:
    t = REPAIR_LABEL.get(kind)
    return (t[0] if lang == "en" else t[1]) if t else kind.replace("_", " ")


# --- English template -> Indonesian template ---------------------------------------------
ID_TEMPLATES = {
    # repair notes
    "Delimiter declared by an Excel 'sep={d}' line.": "Pemisah ditentukan oleh baris Excel 'sep={d}'.",
    "No delimiter detected: treated as a single-column text file.": "Tidak ada pemisah: dibaca sebagai teks satu kolom.",
    "Delimiter detected: {d} with {m} columns (consistency {c:.0%}).": "Pemisah terdeteksi: {d} dengan {m} kolom (konsistensi {c:.0%}).",
    'Backslash-escaped quotes (\\") detected.': 'Tanda kutip dengan garis miring terbalik (\\") terdeteksi.',
    "No header row detected: generic column names generated.": "Baris judul kolom tidak ditemukan: nama kolom umum dibuat.",
    "File encoding detected as {enc}; converted to UTF-8.": "Pengodean file terdeteksi {enc}; dikonversi ke UTF-8.",
    "{n:,} malformed lines rejected by the native reader were repaired and kept.":
        "{n:,} baris rusak yang ditolak pembaca cepat telah diperbaiki dan disimpan.",
    "Parsed with the native DuckDB CSV reader (parallel, out-of-core).": "Dibaca dengan pembaca CSV DuckDB (paralel, hemat memori).",
    "Native reader could not parse the file: switched to the streaming repair parser.":
        "Pembaca cepat gagal: beralih ke pembaca dengan perbaikan per baris.",
    "Row limit reached: only the first {n:,} rows were profiled.": "Batas baris tercapai: hanya {n:,} baris pertama yang dianalisis.",
    "{k:,} rows hold values that do not match their column type; too many to be structural errors, so they were kept as parsed (see mixed-type warnings).":
        "{k:,} baris berisi nilai yang tidak sesuai tipe kolomnya; terlalu banyak untuk disebut kesalahan struktur, jadi dibiarkan (lihat peringatan tipe campuran).",
    "{k:,} rows had values contradicting their column type; {c:,} of them were realigned.":
        "{k:,} baris berisi nilai yang tidak sesuai tipe kolom; {c:,} di antaranya telah disejajarkan ulang.",
    "Sheet '{s}' is hidden in the workbook.": "Sheet '{s}' disembunyikan di dalam workbook.",
    "Columns {cols} contain formulas without saved results: their values are missing in this report.":
        "Kolom {cols} berisi rumus tanpa hasil tersimpan: nilainya tidak ada di laporan ini.",
    "Very large sheet streamed row by row: multi-table detection disabled.":
        "Sheet sangat besar dibaca per baris: deteksi banyak tabel dinonaktifkan.",
    "Sheet/page holds {n} separate tables; this is table {i} ({rng}).": "Sheet/halaman berisi {n} tabel terpisah; ini tabel ke-{i} ({rng}).",
    "Nested objects/arrays are profiled as JSON text.": "Objek/array bersarang dianalisis sebagai teks JSON.",
    "XML records taken from <{tag}> elements.": "Data XML diambil dari elemen <{tag}>.",
    "Table extracted from PDF pages {a}–{b}.": "Tabel diambil dari halaman PDF {a}–{b}.",
    "{n} borderless table(s) detected from text alignment.": "{n} tabel tanpa garis terdeteksi dari perataan teks.",
    "{n} page(s) read with the TableFormer table-structure model (Docling).":
        "{n} halaman dibaca dengan model struktur tabel TableFormer (Docling).",
    "The table-structure model was used on the first {n} unclear pages only.":
        "Model struktur tabel hanya dipakai pada {n} halaman tidak jelas pertama.",
    "Table model failed on page {p}: {e}": "Model tabel gagal di halaman {p}: {e}",
    "Column layout of {n} page(s) worked out by the AI vision model; values come from the PDF text.":
        "Tata letak kolom {n} halaman ditentukan oleh model AI penglihatan; nilainya diambil dari teks PDF.",
    "Table continues over {n} pages; the header repeated on each page was removed.":
        "Tabel berlanjut di {n} halaman; judul kolom yang dicetak ulang di tiap halaman dihapus.",
    "{n} scanned page(s) read with OCR; expect recognition errors.": "{n} halaman pindaian dibaca dengan OCR; mungkin ada salah baca.",
    "No tables found in the PDF; the text layer was parsed as delimited / fixed-width text.":
        "Tidak ada tabel di PDF; teks dibaca sebagai teks berpemisah / lebar tetap.",
    "Read natively by Spark as {fmt}.": "Dibaca langsung oleh Spark sebagai {fmt}.",
    "Spark engine parses line by line: quoted values spanning several lines are not re-joined.":
        "Mesin Spark membaca per baris: nilai berkutip yang melewati beberapa baris tidak digabung.",
    # column notes
    "Numeric-looking code with leading zeros: kept as text.": "Kode berbentuk angka dengan nol di depan: tetap sebagai teks.",
    "Numbers stored as text.": "Angka disimpan sebagai teks.",
    "Formatted numbers converted: '.' thousands / ',' decimal (e.g. 1.234,56); currency symbols, %, (negatives) and footnote marks handled.":
        "Angka berformat dikonversi: '.' ribuan / ',' desimal (mis. 1.234,56); simbol mata uang, %, (negatif) dan tanda catatan ditangani.",
    "Formatted numbers converted: ',' thousands / '.' decimal; currency symbols, %, (negatives) and footnote marks handled.":
        "Angka berformat dikonversi: ',' ribuan / '.' desimal; simbol mata uang, %, (negatif) dan tanda catatan ditangani.",
    "Value and unit merged in one field; numeric part extracted.": "Nilai dan satuan dalam satu sel; bagian angkanya diambil.",
    "Dates stored as text.": "Tanggal disimpan sebagai teks.",
    "Dates stored as text, format detected: {fmt}": "Tanggal disimpan sebagai teks, format terdeteksi: {fmt}",
    "The column name mentions a unit or scale: '{u}'.": "Nama kolom menyebut satuan atau skala: '{u}'.",
    # flags
    "Column is completely empty.": "Kolom ini sepenuhnya kosong.",
    "{pct:.1f}% of values are empty.": "{pct:.1f}% nilai kosong.",
    "{n:,} placeholder values (e.g. 'N/A', '-', 'null') counted as empty.": "{n:,} nilai pengganti (mis. 'N/A', '-', 'null') dihitung sebagai kosong.",
    "Every row has the same value.": "Semua baris bernilai sama.",
    "{n:,} values have extra spaces at the start or end.": "{n:,} nilai memiliki spasi berlebih di awal atau akhir.",
    "Almost all rows have the same value ('{v}' = {pct:.1f}%).": "Hampir semua baris bernilai sama ('{v}' = {pct:.1f}%).",
    "{n:,} values contain invisible characters (special spaces or control characters).":
        "{n:,} nilai mengandung karakter tak terlihat (spasi khusus atau karakter kontrol).",
    "{n:,} values contain line breaks (Enter inside a cell).": "{n:,} nilai mengandung baris baru (Enter di dalam sel).",
    "{n:,} values have garbled characters (e.g. 'Ã©' instead of 'é').": "{n:,} nilai memiliki karakter rusak (mis. 'Ã©' bukan 'é').",
    "{pct:.0f}% of values are in scientific notation (e.g. 1.23E+15).": "{pct:.0f}% nilai dalam notasi ilmiah (mis. 1,23E+15).",
    "Day and month could be swapped (most days are 12 or less); assumed format {fmt}.":
        "Hari dan bulan bisa tertukar (kebanyakan hari bernilai 12 atau kurang); format yang dipakai {fmt}.",
    "{n:,} non-numeric values ({pct:.1f}%) in a numeric column.": "{n:,} nilai bukan angka ({pct:.1f}%) di kolom angka.",
    "{n:,} unusually small or large values ({pct:.2f}%) outside the typical range [{lo}, {hi}].":
        "{n:,} nilai sangat kecil atau sangat besar ({pct:.2f}%) di luar rentang wajar [{lo}, {hi}].",
    "Most values are small with a few very large ones (right-skewed, skew {s:.2f}).":
        "Sebagian besar nilai kecil dengan beberapa nilai sangat besar (condong kanan, skew {s:.2f}).",
    "Most values are large with a few very small ones (left-skewed, skew {s:.2f}).":
        "Sebagian besar nilai besar dengan beberapa nilai sangat kecil (condong kiri, skew {s:.2f}).",
    "{n:,} negative values in a column that is otherwise positive.": "{n:,} nilai negatif di kolom yang umumnya positif.",
    "Looks like dates stored as Excel serial numbers (e.g. {num} = {date}).":
        "Tampaknya tanggal disimpan sebagai nomor seri Excel (mis. {num} = {date}).",
    "Possible placeholder value {v} appears {n:,} times.": "Kemungkinan nilai pengganti {v} muncul {n:,} kali.",
    "{n:,} values could not be read as dates.": "{n:,} nilai tidak bisa dibaca sebagai tanggal.",
    "{n:,} dates are in the future.": "{n:,} tanggal berada di masa depan.",
    "{n:,} dates before 1900 (possible placeholder like 1900-01-01).": "{n:,} tanggal sebelum 1900 (kemungkinan pengganti seperti 1900-01-01).",
    "{n} values are written in several ways (upper/lower case or spaces), e.g. {ex}.":
        "{n} nilai ditulis dengan beberapa cara (huruf besar/kecil atau spasi), mis. {ex}.",
    "Each value seems to hold {k} pieces of information joined by {sep} ({pct:.0f}% of values).":
        "Setiap nilai tampaknya berisi {k} informasi yang digabung dengan {sep} ({pct:.0f}% nilai).",
    "Mixed content: {pct:.0f}% numbers, the rest text.": "Isi campuran: {pct:.0f}% angka, sisanya teks.",
    "Exactly {n:,} rows = an Excel sheet row limit: the data was probably cut off when exported through Excel.":
        "Tepat {n:,} baris = batas baris Excel: data kemungkinan terpotong saat diekspor lewat Excel.",
    "{n} columns are time periods ({cols}, …): the table is in a wide / pivoted layout.":
        "{n} kolom berupa periode waktu ({cols}, …): tabel dalam tata letak melebar / pivot.",
    "Few rows, many columns and a unique label column: the table may be turned sideways (records stored as columns).":
        "Sedikit baris, banyak kolom, dan satu kolom label unik: tabel mungkin terbalik (data disimpan per kolom).",
    # AI anomaly review
    "{n} groups of values probably mean the same thing, e.g. {ex}.": "{n} kelompok nilai kemungkinan berarti sama, mis. {ex}.",
    "These values look like 'no data' placeholders: {vals}.": "Nilai ini tampak seperti pengganti 'tanpa data': {vals}.",
    "{n} values do not seem to fit this column, e.g. {ex}.": "{n} nilai tampaknya tidak cocok untuk kolom ini, mis. {ex}.",
    "AI suggests the name: {name}": "AI menyarankan nama: {name}",
    # narrative
    "This column is empty.": "Kolom ini kosong.",
    "Looks like an ID number: almost every row has its own value ({d:,} different values).":
        "Tampaknya nomor ID: hampir setiap baris punya nilai sendiri ({d:,} nilai berbeda).",
    "Whole numbers from {lo} to {hi} ({d:,} different values).": "Bilangan bulat dari {lo} sampai {hi} ({d:,} nilai berbeda).",
    "Decimal numbers from {lo} to {hi} ({d:,} different values).": "Bilangan desimal dari {lo} sampai {hi} ({d:,} nilai berbeda).",
    "The middle value is {med} and the average is {mean}.": "Nilai tengahnya {med} dan rata-ratanya {mean}.",
    "Values are spread fairly evenly around the average.": "Nilai tersebar cukup merata di sekitar rata-rata.",
    "Most values are on the low side, with a few much higher ones.": "Sebagian besar nilai rendah, dengan beberapa nilai jauh lebih tinggi.",
    "Most values are on the high side, with a few much lower ones.": "Sebagian besar nilai tinggi, dengan beberapa nilai jauh lebih rendah.",
    "Unit: {u}.": "Satuan: {u}.",
    "Dates from {a} to {b} (about {days:,} days).": "Tanggal dari {a} sampai {b} (sekitar {days:,} hari).",
    "Includes the time of day.": "Termasuk jam.",
    "Busiest day of the week: {wd}.": "Hari tersibuk dalam seminggu: {wd}.",
    "Yes/No column: {t:.1f}% yes, {f:.1f}% no.": "Kolom Ya/Tidak: {t:.1f}% ya, {f:.1f}% tidak.",
    "A category column with {d:,} different values.": "Kolom kategori dengan {d:,} nilai berbeda.",
    "Most common: {tops}.": "Paling sering: {tops}.",
    "Looks like a code or ID: almost every row has its own value.": "Tampaknya kode atau ID: hampir setiap baris punya nilai sendiri.",
    "Free text, about {w:.0f} words per entry on average.": "Teks bebas, rata-rata sekitar {w:.0f} kata per isian.",
    "Text with {d:,} different values, {a}–{b} characters long.": "Teks dengan {d:,} nilai berbeda, panjang {a}–{b} karakter.",
    "Most values follow the format {pat} ({pct:.0f}%).": "Sebagian besar nilai mengikuti format {pat} ({pct:.0f}%).",
    "{pct:.1f}% of the rows are empty.": "{pct:.1f}% baris kosong.",
    "No empty values.": "Tidak ada nilai kosong.",
    "Contains {sem}.": "Berisi {sem}.",
}

# --- fixed labels (report and web UI) -------------------------------------------------------
UI = {
    "en": {
        "report_title": "Data check report", "file": "File", "generated": "Created", "lang_name": "English",
        "help": "Help", "tour_start": "Show me around",
        "summary_title": "Summary", "rows": "rows (records)", "cols": "columns (fields)",
        "score": "Data quality score", "score_good": "Good", "score_fair": "Fair", "score_bad": "Needs attention",
        "score_hint": "An indicative score based on empty values, problems found and duplicates.",
        "fact_size": "Your file has {rows} rows and {cols} columns.",
        "fact_repairs": "We automatically fixed {n} problems in the file's layout so it could be read correctly.",
        "fact_norepairs": "The file's layout was clean; nothing needed fixing.",
        "fact_missing": "{pct} of all cells are empty.",
        "fact_dups": "{n} rows appear more than once (duplicates).",
        "fact_nodups": "No duplicate rows.",
        "fact_attention": "{n} columns need your attention.",
        "fact_ok": "No serious problems found in the columns.",
        "tables_found": "This file contains {n} tables", "table": "Table",
        "ai_summary": "AI summary", "ai_about": "What this data is about", "ai_obs": "What the AI noticed",
        "ai_note": "Written by a local AI model. It can make mistakes: double-check important points.",
        "check_title": "What should I check?", "check_none": "Nothing urgent. 🎉",
        "why": "Why it matters", "todo": "What to do", "column": "Column", "examples": "Examples",
        "fixes_title": "What we fixed automatically", "fixes_none": "No fixes were needed.",
        "show_examples": "show examples", "line": "line", "tech_details": "Technical details",
        "columns_title": "The columns one by one", "filter": "Search a column…", "all_types": "All types",
        "only_issues": "Only columns with issues",
        "what_is": "What is in this column", "ai_desc": "AI description",
        "filled": "Filled", "empty": "Empty", "different": "Different values",
        "min": "Smallest", "max": "Largest", "avg": "Average", "median": "Middle value (median)",
        "typical": "Typical range (middle 50%)", "total": "Total", "earliest": "Earliest", "latest": "Latest",
        "span": "Time span", "days": "days", "yes": "Yes", "no": "No", "length": "Length (characters)",
        "distribution": "How values are spread", "by_weekday": "By day of the week",
        "top_values": "Most common values", "from_sample": "(from a sample)", "formats": "Common formats",
        "formats_hint": "A = capital letter, a = small letter, 9 = digit",
        "more": "More details (for analysts)", "std": "Standard deviation", "pctiles": "1% / 5% / 95% / 99%",
        "skewk": "Skew / kurtosis", "zeros_neg": "Zeros / negatives", "outliers": "Unusual values",
        "physical": "Stored as", "issues": "Things to check", "no_issues": "No issues found.",
        "correlations": "Columns that move together",
        "corr_hint": "When one goes up, the other tends to go up (positive) or down (negative).",
        "ai_label": "AI", "ai_suggests": "AI suggestion", "ai_rows": "AI second opinion on hard-to-fix rows",
        "ai_rows_hint": "Rows where the automatic repair had to choose between similar options. The AI's reading is shown for you to compare.",
        "auto_fix": "Our repair", "ai_fix": "AI reading", "agree": "same", "differ": "different",
        "run_notes": "Notes", "engine": "Engine", "duration": "Analysis time", "seconds": "s",
        "approx": "This is a very large table: some numbers are estimates.",
        "glossary": "Glossary",
        "g_row": "Row (record): one line of the table, e.g. one order or one customer.",
        "g_col": "Column (field): one kind of information, e.g. 'date' or 'amount'.",
        "g_median": "Median: the middle value when all values are sorted; less affected by extremes than the average.",
        "g_dup": "Duplicate: a row that appears more than once with exactly the same values.",
        "g_outlier": "Unusual value (outlier): a value much smaller or larger than most others.",
        "back_top": "Back to top", "close": "Close", "next": "Next", "prev": "Back", "done": "Got it",
        "verdict_good": "Ready to use: no serious problems found.",
        "verdict_fair": "Usable after a few fixes.",
        "verdict_bad": "Needs cleaning before you rely on it.",
        "most_important": "Most important", "key_findings": "Key findings", "more_checks": "Other things to check",
        "facts_title": "The facts", "background": "Background details",
        "sort_importance": "Most important first", "sort_file": "File order",
        "see_column": "see column",
    },
    "id": {
        "report_title": "Laporan pemeriksaan data", "file": "File", "generated": "Dibuat", "lang_name": "Bahasa Indonesia",
        "help": "Bantuan", "tour_start": "Tunjukkan caranya",
        "summary_title": "Ringkasan", "rows": "baris (data)", "cols": "kolom (isian)",
        "score": "Skor kualitas data", "score_good": "Baik", "score_fair": "Cukup", "score_bad": "Perlu perhatian",
        "score_hint": "Skor perkiraan berdasarkan nilai kosong, masalah yang ditemukan, dan data ganda.",
        "fact_size": "File Anda berisi {rows} baris dan {cols} kolom.",
        "fact_repairs": "Kami otomatis memperbaiki {n} masalah tata letak file agar bisa dibaca dengan benar.",
        "fact_norepairs": "Tata letak file sudah rapi; tidak ada yang perlu diperbaiki.",
        "fact_missing": "{pct} dari semua sel kosong.",
        "fact_dups": "{n} baris muncul lebih dari sekali (data ganda).",
        "fact_nodups": "Tidak ada baris ganda.",
        "fact_attention": "{n} kolom perlu Anda periksa.",
        "fact_ok": "Tidak ditemukan masalah serius pada kolom.",
        "tables_found": "File ini berisi {n} tabel", "table": "Tabel",
        "ai_summary": "Ringkasan AI", "ai_about": "Tentang data ini", "ai_obs": "Catatan dari AI",
        "ai_note": "Ditulis oleh model AI lokal. AI bisa keliru: periksa kembali hal yang penting.",
        "check_title": "Apa yang perlu saya periksa?", "check_none": "Tidak ada yang mendesak. 🎉",
        "why": "Mengapa penting", "todo": "Yang perlu dilakukan", "column": "Kolom", "examples": "Contoh",
        "fixes_title": "Yang kami perbaiki otomatis", "fixes_none": "Tidak ada yang perlu diperbaiki.",
        "show_examples": "lihat contoh", "line": "baris", "tech_details": "Detail teknis",
        "columns_title": "Penjelasan tiap kolom", "filter": "Cari kolom…", "all_types": "Semua tipe",
        "only_issues": "Hanya kolom bermasalah",
        "what_is": "Isi kolom ini", "ai_desc": "Penjelasan AI",
        "filled": "Terisi", "empty": "Kosong", "different": "Nilai berbeda",
        "min": "Terkecil", "max": "Terbesar", "avg": "Rata-rata", "median": "Nilai tengah (median)",
        "typical": "Rentang umum (50% tengah)", "total": "Jumlah", "earliest": "Paling awal", "latest": "Paling akhir",
        "span": "Rentang waktu", "days": "hari", "yes": "Ya", "no": "Tidak", "length": "Panjang (karakter)",
        "distribution": "Sebaran nilai", "by_weekday": "Per hari dalam seminggu",
        "top_values": "Nilai paling sering", "from_sample": "(dari sampel)", "formats": "Format umum",
        "formats_hint": "A = huruf besar, a = huruf kecil, 9 = angka",
        "more": "Detail lanjutan (untuk analis)", "std": "Simpangan baku", "pctiles": "1% / 5% / 95% / 99%",
        "skewk": "Skew / kurtosis", "zeros_neg": "Nol / negatif", "outliers": "Nilai tidak biasa",
        "physical": "Disimpan sebagai", "issues": "Hal yang perlu diperiksa", "no_issues": "Tidak ada masalah.",
        "correlations": "Kolom yang bergerak bersama",
        "corr_hint": "Jika satu naik, yang lain cenderung ikut naik (positif) atau turun (negatif).",
        "ai_label": "AI", "ai_suggests": "Saran AI", "ai_rows": "Pendapat kedua AI untuk baris yang sulit diperbaiki",
        "ai_rows_hint": "Baris di mana perbaikan otomatis harus memilih di antara pilihan yang mirip. Hasil baca AI ditampilkan sebagai pembanding.",
        "auto_fix": "Perbaikan kami", "ai_fix": "Hasil baca AI", "agree": "sama", "differ": "berbeda",
        "run_notes": "Catatan", "engine": "Mesin", "duration": "Waktu analisis", "seconds": "dtk",
        "approx": "Tabel ini sangat besar: sebagian angka merupakan perkiraan.",
        "glossary": "Istilah",
        "g_row": "Baris (data): satu baris tabel, mis. satu pesanan atau satu pelanggan.",
        "g_col": "Kolom (isian): satu jenis informasi, mis. 'tanggal' atau 'jumlah'.",
        "g_median": "Median: nilai tengah saat semua nilai diurutkan; tidak terlalu terpengaruh nilai ekstrem.",
        "g_dup": "Data ganda: baris yang muncul lebih dari sekali dengan nilai persis sama.",
        "g_outlier": "Nilai tidak biasa (outlier): nilai yang jauh lebih kecil atau lebih besar dari kebanyakan.",
        "back_top": "Kembali ke atas", "close": "Tutup", "next": "Lanjut", "prev": "Kembali", "done": "Mengerti",
        "verdict_good": "Siap digunakan: tidak ada masalah serius.",
        "verdict_fair": "Bisa digunakan setelah beberapa perbaikan.",
        "verdict_bad": "Perlu dibersihkan sebelum diandalkan.",
        "most_important": "Paling penting", "key_findings": "Temuan utama", "more_checks": "Hal lain yang perlu diperiksa",
        "facts_title": "Fakta data", "background": "Detail latar belakang",
        "sort_importance": "Paling penting dulu", "sort_file": "Urutan file",
        "see_column": "lihat kolom",
    },
}

# Guided tour of the report: (CSS selector, title, text)
REPORT_TOUR = {
    "en": [
        ("#tour-summary", "1. Start here: the verdict", "The top of the report gives the conclusion first: can you use this data, the single most important problem, and the key findings with what to do."),
        ("#tour-check", "2. Other things to check", "Smaller problems, each with why it matters and what you can do. Click a column name to jump to it."),
        ("#tour-columns", "3. Each column", "One card per column, the ones needing attention first: what it contains, the most common values, a small chart and anything to check."),
        ("#tour-fixes", "4. Background", "At the bottom: layout problems we repaired automatically (shifted rows, merged cells…), technical notes and a glossary."),
        ("#tour-lang", "5. Language", "Switch between English and Bahasa Indonesia at any time. Click 'Help' to see this tour again."),
    ],
    "id": [
        ("#tour-summary", "1. Mulai dari sini: kesimpulan", "Bagian atas laporan langsung memberi kesimpulan: apakah data bisa dipakai, satu masalah terpenting, dan temuan utama beserta cara mengatasinya."),
        ("#tour-check", "2. Hal lain yang perlu diperiksa", "Masalah yang lebih kecil, masing-masing dengan alasan dan cara mengatasinya. Klik nama kolom untuk melompat ke kolom itu."),
        ("#tour-columns", "3. Setiap kolom", "Satu kartu per kolom, yang perlu perhatian ditampilkan lebih dulu: isinya, nilai paling sering, grafik kecil, dan hal yang perlu diperiksa."),
        ("#tour-fixes", "4. Latar belakang", "Di bagian bawah: masalah tata letak yang kami perbaiki otomatis (baris bergeser, sel gabungan…), catatan teknis, dan daftar istilah."),
        ("#tour-lang", "5. Bahasa", "Ganti antara English dan Bahasa Indonesia kapan saja. Klik 'Bantuan' untuk melihat panduan ini lagi."),
    ],
}
