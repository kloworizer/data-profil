"""Generate a corpus of real-world-style anomalies for Excel, PDF and CSV.

    python scripts/make_anomaly_samples.py [out_dir]

PDF generation needs reportlab (dev-only: pip install reportlab).
"""
import random
import sys
from datetime import date, timedelta
from pathlib import Path

random.seed(11)
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "samples/anomalies")


def workbook(path: Path):
    import openpyxl
    from openpyxl.styles import Font

    wb = openpyxl.Workbook()

    # Sheet 1: title, notes, 2-level merged header, spacer column, section rows,
    # subtotal rows, footnotes, uncached formulas, formatted numbers
    ws = wb.active
    ws.title = "Penjualan"
    ws["A1"] = "LAPORAN PENJUALAN 2024"
    ws.merge_cells("A1:H1")
    ws["A2"] = "Dicetak: 01/07/2024 - Confidential"
    ws.append([])
    ws.append(["No", "Produk", None, "Penjualan", "Penjualan", "Penjualan", "Tgl", "Kode"])
    ws.merge_cells("D4:F4")
    ws.append(["No", "Produk", None, "Qty", "Harga (Rp)", "Total (Rp)", "Tgl", "Kode"])
    r = 6
    no = 1
    for region in ["Wilayah: Jawa", "Wilayah: Sumatera", "Wilayah: Bali"]:
        ws.cell(r, 1, region).font = Font(bold=True)
        r += 1
        first = r
        for _ in range(random.randint(25, 40)):
            qty = random.randint(1, 50)
            price = random.choice([15000, 27500, 125000, 1250000])
            ws.cell(r, 1, no)
            ws.cell(r, 2, random.choice(["Kopi Arabika", "Teh Hijau", "Gula Aren", "kopi arabika ", "Teh Hijau"]))
            ws.cell(r, 4, qty)
            ws.cell(r, 5, f"Rp {price:,.0f}".replace(",", "."))          # Indonesian text-formatted currency
            ws.cell(r, 6, f"=D{r}*{price}")                               # formula, never calculated
            ws.cell(r, 7, (date(2024, 1, 1) - date(1899, 12, 30)).days + random.randint(0, 180))  # serial date
            ws.cell(r, 8, f"{random.randint(10**14, 10**15):.5E}")       # long id mangled to sci notation
            r += 1
            no += 1
        ws.cell(r, 1, "Subtotal")
        ws.cell(r, 4, f"=SUM(D{first}:D{r - 1})")
        ws.cell(r, 5, 0)
        r += 1
    ws.cell(r, 1, "Grand Total")
    ws.cell(r, 4, 999)
    r += 2
    ws.cell(r, 1, "Sumber: Sistem ERP, data belum diaudit")
    ws.cell(r + 1, 1, "* Harga sudah termasuk PPN")

    # Sheet 2: two tables stacked, and one side by side
    ws2 = wb.create_sheet("Multi")
    ws2.append(["Cabang", "Kota", "Karyawan", None, "Kode", "Deskripsi"])
    for i in range(12):
        ws2.append([f"CB{i:02d}", random.choice(["Jakarta", "Bandung", "Medan"]), random.randint(5, 80),
                    None, f"K{i}", f"Kategori {i}"])
    ws2.append([])
    ws2.append([])
    ws2.append(["Tabel 2: Target"])
    ws2.append(["Bulan", "Target", "Realisasi"])
    for mth in range(1, 13):
        ws2.append([date(2024, mth, 1), random.randint(100, 200) * 1000, f"({random.randint(1, 50) * 100:,})"])

    # Sheet 3: wide / pivoted layout with European numbers, NBSP, mojibake, ambiguous dates
    ws3 = wb.create_sheet("Wide")
    ws3.append(["Produk", "2019", "2020", "2021", "2022", "2023", "Catatan", "Tanggal Update"])
    for i in range(30):
        d = date(2023, random.randint(1, 12), random.randint(1, 12))
        ws3.append([f"Produk {i}"] + [f"{random.uniform(1000, 99999):,.2f}".replace(",", "X").replace(".", ",")
                                      .replace("X", ".") for _ in range(5)] +
                   [random.choice(["CafÃ© latte", "Normal", "crème", "Normal​"]), d.strftime("%d/%m/%Y")])

    # Sheet 4: hidden helper sheet
    ws4 = wb.create_sheet("Helper")
    ws4.sheet_state = "hidden"
    ws4.append(["key", "value"])
    for i in range(10):
        ws4.append([i, i * i])
    wb.save(path)


def pdf(path: Path):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    styles = getSampleStyleSheet()

    def furniture(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.drawString(40, 815, "PT Contoh Data Tbk - Laporan Internal")
        canvas.drawRightString(555, 20, f"Page {doc.page} of 3")
        canvas.restoreState()

    hdr = [["Region", "Product", "Sales", "", "Remarks"], ["", "", "Q1", "Q2", ""]]
    rows = []
    for i in range(70):
        rows.append([random.choice(["North", "South", "East"]), f"Item {i}", f"{random.randint(1, 900):,}",
                     f"({random.randint(1, 90)})" if i % 9 == 0 else f"{random.randint(1, 900):,}",
                     "Long remark that wraps onto the next line inside the cell" if i % 7 == 0 else "ok"])
    data = hdr + [[r[0], r[1], r[2], r[3], Paragraph(r[4], styles["BodyText"])] for r in rows]
    t = Table(data, repeatRows=2, colWidths=[70, 80, 60, 60, 200])
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                           ("SPAN", (2, 0), (3, 0)), ("SPAN", (0, 0), (0, 1)), ("SPAN", (1, 0), (1, 1)),
                           ("SPAN", (4, 0), (4, 1))]))
    story = [Paragraph("Quarterly Sales", styles["Title"]), t, PageBreak(),
             Paragraph("Borderless summary", styles["Heading2"]), Spacer(1, 10)]
    bl = [["Code", "Description", "Units", "Amount"]] + [
        [f"C{i:03d}", random.choice(["Widget", "Gadget", "Gizmo"]), str(random.randint(1, 99)),
         f"{random.uniform(10, 9999):,.2f}"] for i in range(25)]
    t2 = Table(bl, colWidths=[60, 140, 60, 80])
    t2.setStyle(TableStyle([("ALIGN", (2, 0), (-1, -1), "RIGHT")]))
    story.append(t2)
    SimpleDocTemplate(str(path), pagesize=A4, topMargin=50, bottomMargin=40).build(
        story, onFirstPage=furniture, onLaterPages=furniture)


def csv_variants(out: Path):
    lines = ["sep=;", "# exported by legacy system", "id;name;amount;note"]
    for i in range(300):
        name = random.choice(['Budi "Bud" Santoso', "Siti", "Agus\x00", "Dewi"])
        name = name.replace('"', '\\"')
        lines.append(f'{i};"{name}";{random.randint(1, 999)},{random.randint(10, 99)};ok')
        if i % 97 == 0:
            lines.append("# checkpoint")
    (out / "excel_sep_escape.csv").write_bytes("\n".join(lines).encode("latin-1", errors="replace"))


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    workbook(OUT / "anomaly_workbook.xlsx")
    csv_variants(OUT)
    try:
        pdf(OUT / "anomaly_report.pdf")
    except ImportError:
        print("reportlab not installed: PDF sample skipped")
    print("written to", OUT.resolve())
