"""Command line: python -m autoeda.cli <file> [-o report.html] [--engine spark] [--no-llm]"""
import argparse
import sys
from pathlib import Path

from . import config
from .pipeline import run


def main(argv=None):
    ap = argparse.ArgumentParser(prog="autoeda", description="No-code automated EDA → HTML report")
    ap.add_argument("path", help="input file (local path; hdfs:// or s3a:// with --engine spark)")
    ap.add_argument("-o", "--out", help="output HTML path (default: <file>.eda.html)")
    ap.add_argument("--engine", choices=["local", "spark"], default=config.ENGINE)
    ap.add_argument("--no-llm", action="store_true", help="skip Ollama descriptions")
    ap.add_argument("--model", default=config.OLLAMA_MODEL)
    ap.add_argument("--max-rows", type=int, default=0, help="profile only the first N rows (0 = all)")
    ap.add_argument("--lang", choices=["en", "id"], default="en", help="language of AI texts / default report language")
    ap.add_argument("--keep-work", action="store_true", help="keep intermediate Parquet files")
    a = ap.parse_args(argv)
    out = Path(a.out) if a.out else Path(Path(a.path.rstrip("/")).name + ".eda.html")
    last = [-1]

    def progress(p, msg):
        pct = int(p * 100)
        if pct != last[0] or p >= 1:
            last[0] = pct
            print(f"[{pct:3d}%] {msg}", file=sys.stderr, flush=True)

    res = run(a.path, out, engine=a.engine, use_llm=not a.no_llm, model=a.model, max_rows=a.max_rows, lang=a.lang,
              progress=progress, keep_work=a.keep_work)
    for w in res["warnings"]:
        print("note:", w, file=sys.stderr)
    print(f"Report: {out.resolve()}  ({res['elapsed_s']:.1f}s)")


if __name__ == "__main__":
    main()
