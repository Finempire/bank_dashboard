# app.py
import io
import re
import uuid
from typing import List, Dict, Optional
from flask import Flask, render_template, request, redirect, url_for, send_file, flash
from werkzeug.utils import secure_filename
import pandas as pd

# PDF / OCR imports
import pdfplumber
from pdf2image import convert_from_bytes
from PIL import Image, ImageOps, ImageFilter, ImageEnhance
import pytesseract

ALLOWED_EXTENSIONS = {"pdf"}

app = Flask(__name__)
app.secret_key = "change_me_in_production"

# Simple in-memory storage for generated files (bytes)
# key -> {"bytes": b..., "filename": "x.xlsx", "mimetype": "..."}
STORE: Dict[str, Dict] = {}

# ---------- Regex patterns (adapted from your uploaded Kotak parser) ----------
DATE_LINE_RE = re.compile(r'^\s*(\d{1,2}[-/]\d{1,2}[-/]\d{2,4}|\d{1,2}\s[A-Za-z]{3}\s\d{4})\s+')
AMOUNT_TAG_RE = re.compile(r'(\d{1,3}(?:[,\s]\d{3})*(?:\.\d{2}))\s*\(?\s*(Dr|Cr)\s*\)?', re.IGNORECASE)
AMOUNT_PLAIN_RE = re.compile(r'(\d{1,3}(?:[,\s]\d{3})*(?:\.\d{2}))')
CHEQUE_REF_RE = re.compile(r'\b(IMPS|UPI|TBMS|NEFT|RTGS|Chq|Cheque|Ref|Reference)[-/]?[A-Za-z0-9]+\b', re.IGNORECASE)

# ---------------- extraction & OCR helpers ----------------
def text_from_pdf_bytes(pdf_bytes: bytes) -> List[str]:
    lines: List[str] = []
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                txt = page.extract_text() or ""
                for ln in txt.splitlines():
                    ln = ln.rstrip()
                    if ln:
                        lines.append(ln)
    except Exception:
        # fallback / ignore, caller may attempt OCR
        pass
    return lines

def preprocess_image_for_ocr(pil_img: Image.Image, sharpen: bool = True, contrast: float = 1.6, threshold: int = 150) -> Image.Image:
    im = pil_img.convert("L")
    if im.size[0] < 1000:
        im = im.resize((int(im.size[0] * 1.5), int(im.size[1] * 1.5)), Image.BILINEAR)
    try:
        enhancer = ImageEnhance.Contrast(im)
        im = enhancer.enhance(contrast)
    except Exception:
        pass
    if sharpen:
        im = im.filter(ImageFilter.SHARPEN)
    im = ImageOps.autocontrast(im, cutoff=1)
    im = im.point(lambda x: 0 if x < threshold else 255, mode='1')
    im = im.convert("L")
    return im

def ocr_pdf_bytes(pdf_bytes: bytes, dpi: int = 300, max_pages: Optional[int] = None) -> List[str]:
    lines: List[str] = []
    try:
        images = convert_from_bytes(pdf_bytes, dpi=dpi)
    except Exception:
        return lines
    if max_pages:
        images = images[:max_pages]
    for img in images:
        proc = preprocess_image_for_ocr(img, sharpen=True, contrast=1.6, threshold=150)
        try:
            txt = pytesseract.image_to_string(proc, lang='eng', config="--psm 6")
        except Exception:
            txt = ""
        page_lines = [l.strip() for l in txt.splitlines() if l.strip()]
        lines.extend(page_lines)
    return lines

# ---------------- parsing logic (Kotak-adapted) ----------------
def group_lines_into_records(lines: List[str]) -> List[str]:
    records: List[str] = []
    for ln in lines:
        if DATE_LINE_RE.match(ln):
            records.append(ln.strip())
        else:
            if records:
                records[-1] = records[-1] + " " + ln.strip()
            else:
                records.append(ln.strip())
    return records

def parse_record(rec: str) -> Optional[Dict[str, str]]:
    m = DATE_LINE_RE.match(rec)
    if not m:
        return None
    date = m.group(1).strip()
    rest = rec[m.end():].strip()
    
    rest_clean = CHEQUE_REF_RE.sub('', rest)
    amount_tags = AMOUNT_TAG_RE.findall(rest_clean)
    if not amount_tags:
        plain_amounts = AMOUNT_PLAIN_RE.findall(rest_clean)
        if len(plain_amounts) >= 2:
            txn_amt = plain_amounts[-2].replace(' ', '').replace(',', '')
            bal_amt = plain_amounts[-1].replace(' ', '').replace(',', '')
            narration = AMOUNT_PLAIN_RE.sub('', rest_clean).strip()
            narration = narration.strip(' ,;-')
            return {"Date": date, "Narration": narration, "Debit": txn_amt, "Credit": "", "Balance": bal_amt}
        else:
            narration = rest_clean
            return {"Date": date, "Narration": narration, "Debit": "", "Credit": "", "Balance": ""}
    
    normalized = [(a.replace(' ', '').replace(',', ''), t.lower()) for a, t in amount_tags]
    txn_amt, txn_tag = normalized[0]
    if len(normalized) >= 2:
        bal_amt, bal_tag = normalized[-1]
    else:
        bal_amt = ""
    debit = txn_amt if txn_tag == 'dr' else ""
    credit = txn_amt if txn_tag == 'cr' else ""
    
    narration = AMOUNT_TAG_RE.sub('', rest_clean).strip()
    narration = narration.strip(' ,;-')
    
    return {"Date": date, "Narration": narration, "Debit": debit, "Credit": credit, "Balance": bal_amt}

def parse_records(records: List[str]) -> pd.DataFrame:
    parsed = []
    for r in records:
        p = parse_record(r)
        if p:
            parsed.append(p)
    df = pd.DataFrame(parsed, columns=["Date", "Narration", "Debit", "Credit", "Balance"])
    df = df.fillna("").astype(str)
    return df

# ---------------- helpers to create downloadable bytes ----------------
def to_excel_bytes(dff: pd.DataFrame) -> bytes:
    out = io.BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        dff.to_excel(writer, index=False, sheet_name="Statement")
    out.seek(0)
    return out.getvalue()

def to_xml_bytes(dff: pd.DataFrame, root_name: str = "Statement", row_name: str = "Transaction") -> bytes:
    # Very simple XML generator. Customize as needed.
    lines = [f'<?xml version="1.0" encoding="UTF-8"?>', f'<{root_name}>']
    for _, row in dff.iterrows():
        lines.append(f'  <{row_name}>')
        for col in dff.columns:
            val = str(row[col]) if pd.notna(row[col]) else ""
            # escape basic xml characters
            val = (val.replace("&", "&amp;")
                      .replace("<", "&lt;")
                      .replace(">", "&gt;")
                      .replace('"', "&quot;")
                      .replace("'", "&apos;"))
            lines.append(f'    <{col}>{val}</{col}>')
        lines.append(f'  </{row_name}>')
    lines.append(f'</{root_name}>')
    xml = "\n".join(lines).encode("utf-8")
    return xml

# ---------------- utility ----------------
def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

# Route: index / upload form
@app.route("/", methods=["GET"])
def index():
    # banks list — add new bank keys and parsers here in the future
    banks = [
        ("kotak", "Kotak Mahindra Bank"),
        ("hdfc", "HDFC Bank (not implemented)"),
        ("icici", "ICICI Bank (not implemented)"),
    ]
    return render_template("index.html", banks=banks, preview=None, info=None)

# Route: process upload
@app.route("/process", methods=["POST"])
def process():
    # Bank choice
    bank = request.form.get("bank")
    file = request.files.get("pdf_file")
    no_ocr = request.form.get("no_ocr") == "on"
    ocr_dpi = int(request.form.get("ocr_dpi") or 350)
    max_ocr_pages = int(request.form.get("max_ocr_pages") or 0)

    if not file or file.filename == "":
        flash("No file selected", "error")
        return redirect(url_for("index"))
    if not allowed_file(file.filename):
        flash("Only PDF files are allowed", "error")
        return redirect(url_for("index"))

    filename = secure_filename(file.filename)
    pdf_bytes = file.read()

    # For now we only have Kotak parser implemented — you can add more
    if bank != "kotak":
        # graceful message
        flash("Selected bank parser not implemented yet. Using Kotak parser as fallback.", "info")

    # 1) Try simple text extraction
    lines = text_from_pdf_bytes(pdf_bytes)

    # 2) If few lines and OCR allowed, do OCR
    if (len(lines) < 8) and (not no_ocr):
        max_pages = max_ocr_pages if max_ocr_pages > 0 else None
        ocr_lines = ocr_pdf_bytes(pdf_bytes, dpi=ocr_dpi, max_pages=max_pages)
        if ocr_lines:
            lines = ocr_lines

    if not lines:
        flash("No text extracted from PDF (text extraction and OCR returned no content).", "error")
        return redirect(url_for("index"))

    # 3) Group into records and parse
    records = group_lines_into_records(lines)
    df = parse_records(records)

    if df.empty:
        flash("No transactions parsed. Try changing OCR settings or use a different PDF.", "error")
        return redirect(url_for("index"))

    # Normalize numeric columns
    df_download = df.copy()
    for col in ["Debit", "Credit", "Balance"]:
        if col in df_download.columns:
            df_download[col] = df_download[col].replace(r'^\s*$', None, regex=True)
            df_download[col] = df_download[col].str.replace(',', '', regex=False).str.replace(' ', '', regex=False)
            df_download[col] = pd.to_numeric(df_download[col], errors='coerce')

    # Create downloadable bytes
    excel_bytes = to_excel_bytes(df_download)
    xml_bytes = to_xml_bytes(df_download)

    # Store in-memory with UUID keys
    excel_id = str(uuid.uuid4())
    xml_id = str(uuid.uuid4())
    STORE[excel_id] = {"bytes": excel_bytes, "filename": f"{bank}_{filename.rsplit('.',1)[0]}.xlsx", "mimetype": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
    STORE[xml_id] = {"bytes": xml_bytes, "filename": f"{bank}_{filename.rsplit('.',1)[0]}.xml", "mimetype": "application/xml"}

    # Prepare preview (first 10 rows)
    preview_html = df.head(10).to_html(classes="preview-table", index=False, escape=False)

    banks = [
        ("kotak", "Kotak Mahindra Bank"),
        ("hdfc", "HDFC Bank (not implemented)"),
        ("icici", "ICICI Bank (not implemented)"),
    ]

    info = {
        "rows_parsed": len(df),
        "excel_id": excel_id,
        "xml_id": xml_id,
        "orig_filename": filename
    }

    return render_template("index.html", banks=banks, preview=preview_html, info=info)

# Route: download files by id
@app.route("/download/<file_id>", methods=["GET"])
def download(file_id):
    meta = STORE.get(file_id)
    if not meta:
        flash("File not found or expired.", "error")
        return redirect(url_for("index"))
    return send_file(
        io.BytesIO(meta["bytes"]),
        download_name=meta["filename"],
        mimetype=meta["mimetype"],
        as_attachment=True
    )

if __name__ == "__main__":
    # development debug mode — shows tracebacks in browser/console
    app.debug = True
    app.run(host="127.0.0.1", port=5000, debug=True)

