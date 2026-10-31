"""Generate messy sample files exercising the anomaly handling.

    python scripts/make_samples.py [out_dir] [--big N_ROWS]
"""
import random
import sys
from datetime import date, timedelta
from pathlib import Path

random.seed(7)
CITIES = ["Jakarta", "Bandung", "Surabaya", "Medan", "Denpasar"]
PRODUCTS = ["Laptop", "Phone", "Tablet", "Monitor", "Keyboard"]


def row(i):
    d = date(2023, 1, 1) + timedelta(days=random.randint(0, 600))
    city = random.choice(CITIES)
    return [str(1000 + i), d.strftime("%d/%m/%Y"), random.choice(PRODUCTS), str(random.randint(1, 20)),
            f"{random.lognormvariate(4, 1):.2f}", f"{city} - {random.randint(10000, 99999)}",
            random.choice(["Y", "N"]), f"{random.randint(40, 95)} kg",
            random.choice(["", "N/A", f"user{i}@mail.com", f"user{i}@mail.com"])]


def messy_csv(path: Path, n=3000):
    hdr = "order_id,order_date,product,qty,amount,location,is_member,weight,email"
    lines = ["Sales export - generated 2024-06-01", "Confidential", "", hdr]
    for i in range(n):
        r = row(i)
        k = random.random()
        if k < 0.01:   # unquoted comma inside product name
            r[2] = r[2] + ", refurbished"
        elif k < 0.02:  # sliding row: extra empty first field, last value lost
            r = [""] + r[:-1]
        elif k < 0.03:  # missing field
            del r[3]
        elif k < 0.035:  # semicolon delimited row
            lines.append(";".join(r))
            continue
        elif k < 0.04:  # trailing delimiters
            lines.append(",".join(r) + ",,")
            continue
        elif k < 0.045:  # amount with text
            r[4] = random.choice(["unknown", "-", "#VALUE!", "12,5"])
        elif k < 0.05:  # two records on one line
            lines.append(",".join(r) + "," + ",".join(row(i + 100000)))
            continue
        elif k < 0.052:  # repeated header
            lines.append(hdr)
        elif k < 0.055:  # case variants
            r[2] = r[2].lower() + " "
        lines.append(",".join(r))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def messy_xlsx(path: Path, n=500):
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sales"
    ws["A1"] = "Quarterly sales report"
    ws.merge_cells("A1:F1")
    ws.append([])
    ws.append(["Order", "Customer", "Customer", "Amount", "Amount", "Date"])
    ws.merge_cells("B3:C3")
    ws.merge_cells("D3:E3")
    ws.append(["id", "name", "segment", "gross", "net", "date"])
    for i in range(n):
        g = round(random.uniform(10, 1000), 2)
        ws.append([i + 1, f"Customer {random.randint(1, 80)}", random.choice(["Retail", "SME", "Corporate"]),
                   g, round(g * 0.89, 2), date(2024, 1, 1) + timedelta(days=random.randint(0, 300))])
    ws2 = wb.create_sheet("Lookup")
    ws2.append(["code", "label"])
    for i in range(20):
        ws2.append([f"C{i:03d}", f"Label {i}"])
    wb.save(path)


def big_csv(path: Path, n: int):
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write("order_id,order_date,product,qty,amount,location,is_member,weight,email\n")
        buf = []
        for i in range(n):
            r = row(i)
            if i % 50_000 == 7:
                r[2] += ", refurbished"   # occasional malformed line
            buf.append(",".join(r))
            if len(buf) >= 100_000:
                f.write("\n".join(buf) + "\n")
                buf = []
        if buf:
            f.write("\n".join(buf) + "\n")


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else "samples")
    out.mkdir(parents=True, exist_ok=True)
    if "--big" in sys.argv:
        n = int(sys.argv[sys.argv.index("--big") + 1])
        big_csv(out / f"big_{n}.csv", n)
    else:
        messy_csv(out / "messy_sales.csv")
        messy_xlsx(out / "messy_report.xlsx")
    print("written to", out.resolve())
