"""Intake: an invoice file (PDF or image) becomes text before Apertus reads it.

A PDF that carries its own text is read as it is; a scan or photo goes through Tesseract OCR (German, French,
Italian, English), run locally as a program, so the invoice never leaves the firm. What follows is unchanged:
the checks compare Apertus's reading against this text, so a value OCR misread is held, not paid
(an IBAN that fails its checksum, a total that is not in the text, net + VAT != total).

`python -m agent ocr` measures it: invoices from the confirm set are drawn as images (clean, and as a skewed,
blurred, compressed scan), read back by OCR, and checked for whether the IBAN, invoice number and total survive.
"""
import io
import json
import os
import random
import re
import subprocess
import tempfile
import zlib
from pathlib import Path

from .common import DATA, RESULTS, iban_valid, normalize_iban, read_jsonl

LANGS = "deu+fra+ita+eng"
TESSERACT = os.environ.get("TESSERACT_CMD", "tesseract")
IMAGE_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/tiff": ".tif"}


def ocr_image(data: bytes, suffix: str = ".png") -> str:
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / f"in{suffix}"
        src.write_bytes(data)
        out = subprocess.run([TESSERACT, str(src), "stdout", "-l", LANGS, "--psm", "6"],
                             capture_output=True, check=True, timeout=120)
    return out.stdout.decode("utf-8", "replace").strip()


def read(data: bytes, content_type: str) -> dict:
    """Return {text, method, pages, image}. method: pdf-text (the PDF's own text) or ocr (Tesseract);
    image: (bytes, mime) of the first page when OCR was used, so Apertus can read the image itself."""
    if content_type == "application/pdf" or data[:5] == b"%PDF-":
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(data)
        pages = [pdf[i] for i in range(len(pdf))]
        text = "\n".join(p.get_textpage().get_text_range() for p in pages).strip()
        if len(re.sub(r"\s", "", text)) >= 40:  # a PDF with real text: no need to guess the letters
            return {"text": text, "method": "pdf-text", "pages": len(pages), "image": None}
        parts, first = [], None
        for p in pages:  # a scanned PDF: render each page at 300 dpi and OCR it
            buf = io.BytesIO()
            p.render(scale=300 / 72).to_pil().save(buf, "PNG")
            first = first or buf.getvalue()
            parts.append(ocr_image(buf.getvalue()))
        return {"text": "\n".join(parts).strip(), "method": "ocr", "pages": len(pages), "image": (first, "image/png")}
    if content_type not in IMAGE_TYPES:
        raise ValueError(f"unsupported file type {content_type!r}: PDF, PNG, JPEG or TIFF")
    return {"text": ocr_image(data, IMAGE_TYPES[content_type]), "method": "ocr", "pages": 1, "image": (data, content_type)}


# --- measurement ------------------------------------------------------------------------------

def draw(text: str, scan: bool = False, seed: int = 0) -> bytes:
    """The invoice as a page image: black text on white at about 200 dpi; scan=True skews, blurs and compresses it."""
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
    font = ImageFont.load_default(size=26)
    lines = text.split("\n")
    w = max(1600, 60 + max(int(font.getlength(x)) for x in lines))
    img = Image.new("L", (w, 80 + 38 * len(lines)), 255)
    d = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        d.text((40, 40 + 38 * i), line, fill=0, font=font)
    buf = io.BytesIO()
    if scan:
        rng = random.Random(seed)
        img = img.rotate(rng.uniform(-1.2, 1.2), expand=True, fillcolor=255).filter(ImageFilter.GaussianBlur(0.9))
        img.save(buf, "JPEG", quality=55)
    else:
        img.save(buf, "PNG")
    return buf.getvalue()


def _survives(text: str, gold: dict) -> dict:
    from .badge import amounts_in
    squashed = re.sub(r"\s", "", text).upper()
    ibans = {normalize_iban(m.group()) for m in re.finditer(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{1,4}){3,8}\b", text)}
    out = {"total": round(gold["total_chf"], 2) in amounts_in(text)}
    out["iban"] = None if not gold.get("iban") else normalize_iban(gold["iban"]) in ibans
    out["invoice_no"] = None if not gold.get("invoice_no") else re.sub(r"\s", "", gold["invoice_no"]).upper() in squashed
    # a misread account that still passes the IBAN checksum is the dangerous kind: count it
    wrong = [i for i in ibans if gold.get("iban") and i != normalize_iban(gold["iban"]) and len(i) == len(normalize_iban(gold["iban"]))]
    out["misread_iban_passes_checksum"] = any(iban_valid(i) for i in wrong)
    return out


def measure(n: int = 300, seed: int = 7) -> dict:
    cases = read_jsonl(DATA / "study_confirm.jsonl")
    sample = random.Random(seed).sample(cases, min(n, len(cases)))
    rows = []
    for c in sample:
        for scan in (False, True):
            text = ocr_image(draw(c["invoice_text"], scan, seed=zlib.crc32(c["case_id"].encode())), ".jpg" if scan else ".png")
            got, src = _survives(text, c["gold"]), _survives(c["invoice_text"], c["gold"])
            for k in ("iban", "invoice_no", "total"):  # a value the invoice itself does not show (unreadable total, pay-by-slip) is not OCR's to read
                if src[k] is not True:
                    got[k] = None
            rows.append({"case_id": c["case_id"], "layout": c.get("layout"), "reason_code": c["reason_code"], "scan": scan, **got})
    report = {"n_invoices": len(sample), "langs": LANGS, "engine": _version(), "seed": seed}
    for scan in (False, True):
        r = [x for x in rows if x["scan"] == scan]
        key = "scan" if scan else "clean"
        rate = lambda k: round(sum(bool(x[k]) for x in r if x[k] is not None) / max(1, sum(x[k] is not None for x in r)), 3)
        report[key] = {"iban": rate("iban"), "invoice_no": rate("invoice_no"), "total": rate("total"),
                       "all_three": round(sum(all(x[k] in (True, None) for k in ("iban", "invoice_no", "total")) for x in r) / len(r), 3),
                       "misread_iban_passes_checksum": sum(x["misread_iban_passes_checksum"] for x in r)}
    report["rows"] = rows
    out = RESULTS / "ocr_study.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "rows"}, indent=2), flush=True)
    return report


def _version() -> str:
    try:
        return subprocess.run([TESSERACT, "--version"], capture_output=True, timeout=30).stdout.decode().split("\n")[0]
    except OSError:
        return "not installed"


# --- Apertus reads the image, code checks its reading against the OCR text -----------------------

def vision_extract(image: bytes, tag: str = "", mime: str = "image/png") -> dict:
    """Apertus 1.5 takes images: it reads the invoice image itself (same extraction prompt, image attached)."""
    import base64
    from . import llm, prompts
    from .common import CATEGORIES
    user = prompts.EXTRACT_USER.format(invoice="(the invoice is the attached image)", categories=", ".join(CATEGORIES))
    msgs = [{"role": "system", "content": prompts.EXTRACT_SYSTEM},
            {"role": "user", "content": [{"type": "text", "text": user}, {"type": "image_url", "image_url": {
                "url": f"data:{mime};base64," + base64.b64encode(image).decode()}}]}]
    try:
        return llm.parse_json(llm.chat(msgs, max_tokens=400, tag=f"vision:{tag}"))
    except Exception as e:  # no reading: the checks fail closed
        return {"error": f"{type(e).__name__}: {e}"}


def measure_vision(n: int = 50, seed: int = 7) -> dict:
    """The first n of the OCR study's sample, clean images: Apertus reads each image, Tesseract reads it too,
    and the code checks (mandate, supplier file, ledger, promoted lessons, in-document) run on Apertus's
    reading with the OCR text as the document. Counts Apertus's misreadings and how many the checks held."""
    from . import badge
    from .badge import _num
    from .common import load_json
    mandates = {m["client_id"]: m for m in load_json(DATA / "mandates.json")}
    vendors = load_json(DATA / "vendors.json")
    cases = random.Random(seed).sample(read_jsonl(DATA / "study_confirm.jsonl"), 300)[:n]
    from concurrent.futures import ThreadPoolExecutor
    imgs = {c["case_id"]: draw(c["invoice_text"]) for c in cases}
    with ThreadPoolExecutor(int(os.environ.get("LLM_CONCURRENCY", "4"))) as ex:  # about a minute per image call
        read = dict(zip(imgs, ex.map(lambda k: vision_extract(imgs[k], k), imgs)))
    rows = []
    for c in cases:
        img = imgs[c["case_id"]]
        text = ocr_image(img)
        f = read[c["case_id"]]
        g = c["gold"]
        right = {"iban": normalize_iban(f.get("vendor_iban")) == normalize_iban(g.get("iban")) if g.get("iban") else None,
                 "total": _num(f.get("total_chf")) is not None and abs(_num(f["total_chf"]) - g["total_chf"]) < 0.01,
                 "invoice_no": (str(f.get("invoice_no") or "").strip().upper() == str(g.get("invoice_no") or "").strip().upper())
                 if g.get("invoice_no") else None}
        m = mandates[c["client_id"]]
        verdict, findings = badge.check(m, vendors.get(c["client_id"], []), c["prior_payments"], f)
        findings += badge.promoted_checks(m, f) + badge.grounding(text, f)
        held = verdict != "pass" or bool(findings)
        wrong = [k for k, v in right.items() if v is False]
        rows.append({"case_id": c["case_id"], "label": c["label"], "reason_code": c["reason_code"], "layout": c.get("layout"),
                     "apertus_wrong": wrong, "error": f.get("error"), "held_by_checks": held,
                     "codes": sorted({x["code"] for x in findings}), "ocr": _survives(text, g)})
    misread = [r for r in rows if r["apertus_wrong"] or r["error"]]
    report = {"n": len(rows), "model": __import__("agent.llm", fromlist=["x"]).model_name(), "seed": seed,
              "apertus_misread_any": len(misread),
              "apertus_misread_by_field": {k: sum(k in r["apertus_wrong"] for r in rows) for k in ("iban", "total", "invoice_no")},
              "misread_and_held": sum(r["held_by_checks"] for r in misread),
              "misread_and_passed": [r["case_id"] for r in misread if not r["held_by_checks"]],
              "ocr_all_three": round(sum(all(r["ocr"][k] in (True, None) for k in ("iban", "invoice_no", "total")) for r in rows) / len(rows), 3),
              "rows": rows}
    (RESULTS / "vision_study.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "rows"}, indent=2), flush=True)
    return report
