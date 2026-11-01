"""PDF ingestion against real-world documents (tests/data/pdf, sources in SOURCES.md).

Each case states what a careful person would extract from the file: the tables, their
page ranges, row counts, column names and the first / last record. Cases marked
`gap=` are known shortcomings: they state the correct result and run as strict
xfail, so a fix shows up as XPASS and the marker has to be removed.

Run: python -m pytest tests -q            (geometry reader only, ~2 min)
     AUTOEDA_TEST_DOCLING=1 python -m pytest tests -q   (with the Docling table model,
     when it is installed)
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import pyarrow.parquet as pq
import pytest

DATA = Path(__file__).parent / "data" / "pdf"


@dataclass
class T:
    pages: str                       # '3-5' (source pages the table was read from)
    rows: int | None = None
    cols: list[str] | None = None
    first: list[str] = field(default_factory=list)   # leading cells of the first record
    last: list[str] = field(default_factory=list)    # leading cells of the last record


@dataclass
class Case:
    file: str
    tables: list[T] = field(default_factory=list)
    exact: bool = True               # False: the file holds more tables than listed
    error: str | None = None         # regex of the expected ValueError
    gap: str | None = None           # known shortcoming (strict xfail)


CASES = [
    # --- borderless tables (text alignment) ------------------------------------------------
    Case("in_population_growth.pdf", [
        T("1-1", 36, ["States/Union Territories", "1951-1961", "1961-1971", "1971-1981", "1981-1991",
                      "1991-2001", "2001-2011"],
          ["Andaman and Nicobar Islands", "105.19", "81.17", "63.93", "48.70", "26.90", "6.68"],
          ["ALL INDIA", "21.51", "24.80", "24.66", "23.87", "21.54", "17.64"])]),
    Case("ar_campaign_donors_borderless.pdf", [
        T("1-1", 55, ["Apellido", "Nombre", "Matricula", "Cuit", "Fecha", "Tipo", "Importe"],
          ["MENA", "JUAN MARTÍN", "27.083.460", "20-27083460-5", "09/10/2013", "EFECTIVO", "$ 10.000,00"],
          ["BOGADO GONZALEZ", "JULIO BERNABE", "27.592.814"])]),
    Case("us_irs_letter_with_table.pdf", [          # cover letter in prose, one small table on p.2
        T("2-2", 8, ["Payroll Period", "Add Additional"], ["Weekly", "$ 40.40"],
          ["Daily or Miscellaneous", "8.10"])]),
    Case("in_state_liabilities_superscript.pdf", [   # three-level header, '1 2= 3 ...' numbering rows
        T("1-1", 30, ["States", "Total / Internal / Debt", "Market / Loans", "NSSF", "WMA / from / RBI",
                      "Loans / from / Banks / & FIs", "Loans / from / LIC", "Loans / from / GIC",
                      "Loans / from / NABARD", "Loans / from SBI / & Other / Banks", "Loans / from / NCDC"],
          ["Andhra Pradesh", "48.11", "40.45", "-", "3.26"], ["ALL STATES", "513.38", "436.02"])],
         gap="second line of the numbering row ('(3 to 6)+14') is kept as a data row"),
    Case("synthetic_anomaly_report.pdf", [            # repo sample: wrapped remarks, 'Item N' cells, 3 pages
        T("1-3", 70, ["Region", "Product", "Sales / Q1", "Sales / Q2", "Remarks"],
          ["North", "Item 0", "557", "(51)", "Long remark that wraps onto the next line inside the cell"],
          ["South", "Item 69", "687", "81", "ok"]),
        T("4-4", 25, ["Code", "Description", "Units", "Amount"], ["C000", "Widget", "24", "3,369.27"],
          ["C024", "Widget", "84", "3,222.72"])]),
    Case("us_crop_trial_shaded_rows.pdf", [          # row shading drawn as rectangles, no cell borders
        T("1-1", 29, None, ["Kruger", "K2-1901", "RR2Y", "1.9", "R", "Ac,PV", "56.4", "7.6", "0", "126.3", "$846"],
          ["Gold Country", "1541", "RR2Y", "1.5"])], exact=False,
         gap="key/value lines above the table (SOIL CONDITIONS: ...) are read into it"),

    # --- ruled tables ---------------------------------------------------------------------------
    Case("us_fuel_savings_twolevel_header.pdf", [
        T("1-1", 5, ["Cycle Name", "KI (1/km)", "Distance (mi)", "Percent Fuel Savings / Improved Speed",
                     "Percent Fuel Savings / Decreased Accel", "Percent Fuel Savings / Eliminate Stops",
                     "Percent Fuel Savings / Decreased Idle"],
          ["2012_2", "3.30", "1.3", "5.9%", "9.5%", "29.2%", "17.4%"],
          ["4171_1", "0.07", "173.9", "58.1%", "1.6%", "2.1%", "0.5%"])]),
    Case("in_accidental_deaths_numbering_row.pdf", [  # '(1) (2) ... (8)' under the header
        T("1-1", 47, ["Sl. No.", "Year", "Population (in Lakh)", "Accidental Deaths / Incidence",
                      "Accidental Deaths / Rate", "Suicides / Incidence", "Suicides / Rate",
                      "Percentage Population growth"],
          ["1.", "1967", "4999", "126762", "25.4", "38829", "7.8", "2.2"],
          ["47.", "2013", "12288", "400517", "32.6", "134799", "11.0", "1.0"])]),
    Case("us_medi_cal_row_spans.pdf", [               # plan type / county merged down over rows
        T("1-1", 37, ["Plan Type", "County", "Plan Name", "Totals"],
          ["GMC", "Sacramento", "Anthem Blue Cross", "164,380"],
          ["All Models Total Enrollments", "", "", "10,132,875"])]),
    Case("in_hiv_campaign_background_lines.pdf", [
        T("1-1", 6, ["State", "Date", "Halt stations", "Halt days", "Persons directly reached (in lakh)",
                     "Persons trained", "Persons counseled", "Persons tested for HIV"],
          ["Delhi", "1.12.2009", "8", "17", "1.29", "3,665", "2,409", "1,000"],
          ["Kerala", "23.2.2010 to 11.3.2010", "9", "17", "1.42", "3,559", "2,173", "855"])]),
    Case("in_rainfall_section_rows.pdf", [            # 'REGION : ...' rows become a section column
        T("1-1", 37, None, ["1", "ARUNACHAL PRADESH", "11.4", "18.4", "-38%", "D", "601.2"],
          ["", "COUNTRY :", "12.3", "9.3", "32%"]),
        T("1-1", 6, ["CATEGORY", "Day:10-07-2016 / NO.OF STATES", "Period:01-06-2016 To 10-07-2016 / NO.OF STATES"],
          ["Excess", "12", "5"], ["NoData", "0", "0"])]),
    Case("us_nics_stacked_rows.pdf", [                # no lines between rows: 5 states per cell
        T("1-1", 55, None, ["Alabama", "18,870", "23,022", "22,650", "859"], ["Wyoming", "383", "1,745", "2,372"])]),
    Case("ar_voting_record.pdf", [
        T("1-1", 31, None, ["ABDALA de MATARAZZO, Norma Amanda", "Frente Cívico por Santiago",
                            "Santiago del Estero", "AFIRMATIVO"],
          ["CURRILEN, Oscar Rubén", "Frente para la Victoria - PJ", "Chubut", "AFIRMATIVO"])]),
    Case("us_servers_spanning_cells.pdf", [
        T("1-1", 15, None, ["Server closets", "1,505", "1,580", "1,643", "1,673", "1,689"],
          ["Enterprise-class data centers", "1,495", "1,572", "1,585", "1,424", "1,485"]),
        T("1-1", 2, ["Server class", "2007", "2008", "2009", "2010", "2011"],
          ["Mid-range", "4,921"], ["High-end", "76,295", "81,624", "86,849", "92,662", "98,864"])]),
    Case("jp_equity_statement_two_tables.pdf", [      # Japanese, two tables side by side
        T("1-1", 9, None, ["当期首残高", "5,664", "749", "12,017", "△747", "17,683"],
          ["当期末残高", "5,664", "749", "12,846", "△747", "18,512"]),
        T("1-1", 9, None, ["当期首残高", "△669", "61", "△109", "―", "△717", "246", "17,212"],
          ["当期末残高", "△113", "142", "△104", "―", "△75", "245", "18,683"])]),
    Case("in_literacy_two_tables.pdf", [              # two tables on a page, boxed titles
        T("1-1", 11, None, ["Kerala", "2400", "7.2", "0.5"], ["Pooled", "23889", "30.9", "1.9"]),
        T("1-1", 11, None, ["Kerala", "2400", "8.8", "0.3"], ["Pooled", "23889", "47.7", "1.5"])],
         gap="second header line ('Write' under 'Read &') is read as a data row"),
    Case("in_district_health_dotted_leaders.pdf", [
        T("1-1", None, None, ["Children age 0-5 months exclusively breastfed", "", "", "NA", "NA"])],
         gap="indicator text with dotted leaders spans the value columns and is copied into them"),

    # --- tables over several pages ------------------------------------------------------------
    Case("us_warn_report_16_pages.pdf", [             # letter-spaced dates, header repeated per page
        T("1-15", 633, ["Notice Date", "Effective", "Received", "Company", "City", "No. Of", "Layoff/Closure"],
          ["06/22/2015", "03/25/2016", "07/01/2015", "Maxim Integrated Product", "San Jose", "150"],
          ["03/21/2016", "05/27/2016", "03/23/2016", "Rockwell Collins, Inc.", "Poway", "2"]),
        T("15-16", 9, None, ["July 2015", "71", "8,574"], ["March 2016", "58", "2,839"])]),
    Case("us_school_donors_rotated_5_pages.pdf", [    # landscape pages
        T("1-5", None, ["Last Name", "First Name", "Address", "City", "State", "Zip", "Occupation",
                        "Employer", "Date", "Amount"],
          ["Lidstad", "Dick & Peg", "62 Mississippi River Blvd N", "Saint Paul", "MN", "55104", "retired"])]),
    Case("gh_contract_awards_rotated.pdf", [
        T("1-5", 44, ["Serial No.", "Contract Description", "Contract No.", "Method of Procurement",
                      "Contractor/Suppliers /Consultant", "Address", "Contract Amount", "Contract Currency",
                      "Date signed Mm/dd/yr", "Estimated Contract completion date Mm/dd/yr", "Remarks"],
          ["1", "Procurement of 33No. (4X4) Pickups, 4No. (4x2) Pickups"],
          ["44", "Construction of Driver Testing Grounds at One (1) Station in the Northern Sector of Ghana ‐ Kumasi"])]),
    Case("eu_survey_replies.pdf", [
        T("3-3", None, ["Country", "Valid replies per country", "Marie Curie replies", "Total"],
          ["Austria", "77", "1", "78"], ["United Kingdom", "572", "14", "586"])], exact=False),

    # --- Indonesian government documents --------------------------------------------------------
    Case("id_bps_kecamatan_dalam_angka.pdf", [        # BPS: bilingual headers, (1)(2)(3) row, watermark
        T("35-35", 13, ["Desa Village", "Luas Total Area (km2/sq.km)",
                        "Persentase terhadap Luas Kecamatan Percentage to District Area"],
          ["Kwangko", "42,42", "15,61"], ["Kecamatan Manggelewa", "271,72", "100"]),
        T("45-46", 13, ["Pemerintah Daerah Local Government", "Laki-Laki Male", "Perempuan Female", "Jumlah Total"],
          ["Pemerintah Daerah Kecamatan Manggelewa Manggelewa District Government", "157", "224", "381"],
          ["Pemerintah Daerah Desa Anamina Anamina Village Government", "17", "1", "18"]),
        T("54-54", 13, ["Desa Village", "Penduduk/Population / Laki-Laki/Male",
                        "Penduduk/Population / Perempuan/Female", "Penduduk/Population / Jumlah/Total"],
          ["Kwangko", "1.216", "1.172", "2.388"], ["Kecamatan Manggelewa", "18.426", "18.286", "36.712"]),
        T("56-56", 17, None, ["0-4", "1.535", "1.485", "3.020"], ["Kecamatan Manggelewa", "16.121", "15.934", "32.055"]),
        # a wide table printed over two pages: same villages, more columns ('Lanjutan Tabel')
        T("81-82", 13, ["Desa Village", "Gempa Bumi Earthquake", "Tsunami Tsunami",
                        "Gunung Meletus Volcanic Eruption", "Tanah Longsor Landslide", "Banjir Floods",
                        "Banjir Bandang Flash Floods", "Kekeringan Drought",
                        "Kebakaran Hutan dan Lahan Forest and Land Fires"],
          ["Kwangko", "–", "–"], ["Kecamatan Manggelewa", "–", "–"])], exact=False),
    Case("id_kemkes_profil_kesehatan.pdf", [
        T("3-3", 17, ["No", "Kelompok Umur (Tahun)", "Jumlah Penduduk / Laki-laki", "Jumlah Penduduk / Perempuan",
                      "Jumlah Penduduk / Laki-laki + Perempuan", "Jumlah Penduduk / Rasio Jenis Kelamin"],
          ["1", "0 - 4", "11.054.661", "10.801.531", "21.856.192", "102,3"],
          ["Angka Beban Tanggungan (Dependency Ratio)", "", "", "", "", "100,7"])], exact=False),
    Case("id_kemkes_profil_kesehatan.pdf", [          # 34 provinces + the national total row
        T("2-2", 35, None, ["1", "Aceh", "56.839,09", "18", "5", "23"], ["Indonesia"])], exact=False,
         gap="last province and the total row share one cell; their characters interleave"),
    Case("id_cpns_pengumuman_no_table.pdf", error="No table found"),

    # --- no table / unreadable --------------------------------------------------------------------
    Case("us_bls_article_no_table.pdf", error="No table found"),
    Case("encrypted.pdf", error="password-protected"),
    Case("scanned_image_only.pdf", error="no extractable text"),
    Case("us_court_transcript_no_table.pdf", error="No table found",
         gap="line-numbered transcript is read as a table"),
    Case("us_senate_rotated_page.pdf", [T("1-1")], exact=False,
         gap="text on a /Rotate 90 page is not turned upright before reading"),
    Case("in_agstat_vertical_header.pdf", [
        T("1-1", 31, None, ["1", "Balasore", "23.65"], ["ODISHA"])], exact=False,
         gap="vertical header text is read backwards ('noitalupoP')"),
    Case("il_indicators_hebrew.pdf", [T("1-1", 30)], exact=False,
         gap="right-to-left text and stacked cells without row lines"),
]


def _ingest(path: Path, tmp: Path):
    from autoeda.ingest import ingest
    return ingest(path, tmp)[0]


def _cells(row: dict, cols: list[str]) -> list[str]:
    return ["" if row[c] is None else str(row[c]) for c in cols]


@pytest.fixture(autouse=True)
def _engine(monkeypatch):
    if not os.getenv("AUTOEDA_TEST_DOCLING"):
        from autoeda import config
        monkeypatch.setattr(config, "PDF_ENGINE", "plumber")


def _id(c: Case) -> str:
    return c.file.removesuffix(".pdf") + ("-gap" if c.gap else "")


@pytest.mark.parametrize("case", [pytest.param(c, id=_id(c), marks=[pytest.mark.xfail(reason=c.gap, strict=True)]
                                               if c.gap else []) for c in CASES])
def test_pdf(case: Case, tmp_path: Path):
    path = DATA / case.file
    if case.error:
        with pytest.raises(ValueError, match=case.error):
            _ingest(path, tmp_path)
        return
    tables = _ingest(path, tmp_path)
    got = []
    for t in tables:
        cols = [c for c in t.columns if c not in t.drop_columns]
        got.append((t, cols, pq.read_table(t.path, columns=cols).to_pylist()))
    if case.exact:
        assert [t.source.get("pages") for t, _, _ in got] == [e.pages for e in case.tables]
    used: set[int] = set()
    for exp in case.tables:
        # the next unused table read from the same pages
        i = next((i for i, (t, _, _) in enumerate(got) if i not in used and t.source.get("pages") == exp.pages), None)
        assert i is not None, f"no table from pages {exp.pages}; got {[t.source.get('pages') for t, _, _ in got]}"
        used.add(i)
        t, cols, rows = got[i]
        where = f"table from pages {exp.pages}"
        if exp.rows is not None:
            assert len(rows) == exp.rows, where
        if exp.cols is not None:
            assert cols == exp.cols, where
        if exp.first:
            assert _cells(rows[0], cols)[:len(exp.first)] == exp.first, where
        if exp.last:
            assert _cells(rows[-1], cols)[:len(exp.last)] == exp.last, where
        if case.file.startswith("in_agstat"):      # names must read left to right
            assert not any(re.search(r"noitalupoP", c) for c in cols)
