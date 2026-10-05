"""
Generates AuraVance's own three enterprise BFSI documents (3 pages each) for
the RAG assistant: the documents its customers and staff will ask about.

    python -m scripts.generate_benchmark_pdfs
    python -m scripts.generate_benchmark_pdfs --legal-name "Other Name Private Limited" \
        --cin U65110MH2026PTC384152 --pan AAACO2026N --domain othername.aura

One legal entity, three regulated roles (a Private Limited company cannot itself
be a bank, an asset manager or an insurer, so the documents reflect roles it can hold):
    Banking pillar            lender registered with the RBI as an NBFC (digital lending)
    Financial Services pillar AMFI-registered mutual fund distributor presenting the CAS
    Insurance pillar          IRDAI-registered corporate agent servicing a partner insurer's policy

Output: samples/company/
    banking_rtl_standard_facility_agreement.pdf
    finserv_mutual_fund_ecas_and_servicing.pdf
    insurance_flexi_commercial_property.pdf

Each document is exactly three pages:
    Page 1  metadata classification table + preliminary legal/operational framework
    Page 2  ruled high-density tables + one embedded raster chart
    Page 3  statutory covenants / disclosures + escalation matrix + audit sign-off

IMPORTANT: officers, registration numbers (RBI CoR, AMFI ARN, IRDAI CA), contact
numbers, rates, grades, NAVs and every figure are ILLUSTRATIVE sample data. The CIN
and PAN are the values supplied for the company. Replace the rest with approved
company data before production use. Regulatory references (RBI, SEBI, AMFI, IRDAI)
follow the public frameworks as understood when this was written; have Compliance
verify them against current circulars. All derived numbers are computed in code.
"""
from __future__ import annotations

import argparse
import io
import re
from datetime import date
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate, Frame, Image as RLImage, KeepInFrame, PageBreak,
    PageTemplate, Paragraph, Spacer, Table, TableStyle,
)
from reportlab.platypus.doctemplate import LayoutError

OUT_DIR = Path(__file__).resolve().parent.parent / "samples" / "company"

NAVY = colors.HexColor("#14284b")
TEAL = colors.HexColor("#1f6f78")
GRID = colors.HexColor("#8a94a6")
BAND = colors.HexColor("#e8edf5")
KEYBG = colors.HexColor("#dfe6f1")

PAGE_W, PAGE_H = A4
MARGIN_X = 14 * mm
TOP = 24 * mm
BOTTOM = 17 * mm
FRAME_W = PAGE_W - 2 * MARGIN_X
FRAME_H = PAGE_H - TOP - BOTTOM


# ───────────────────────── company identity ─────────────────────────
class Co:
    """Everything company-specific, derived from the supplied identity."""

    def __init__(self, legal: str, cin: str, pan: str, domain: str, short: str | None = None):
        self.legal = legal.strip()
        self.short = (short or self.legal.split()[0]).strip()
        self.cin, self.pan = cin.strip(), pan.strip()
        self.dom = domain.strip().lstrip("@")
        caps = "".join(re.findall(r"[A-Z]", self.short))
        self.ini = (caps if len(caps) >= 2 else self.short[:2].upper())[:4]
        self.house = f"{self.short} Tower, Bandra Kurla Complex, Mumbai, Maharashtra 400051"
        # illustrative registrations and contacts: replace with the real ones
        self.nbfc_cor, self.arn, self.ca_reg = "N-13.02147", "ARN-147236", "CA 147"
        self.partner_insurer = "Himavat General Insurance Company Limited"
        self.lend_tf, self.inv_tf, self.ins_tf = "1800 000 1100", "1800 000 2200", "1800 000 3300"


CO: Co = Co("AuraVance Digital FinTech Private Limited", "U65110MH2026PTC384152", "AAACF2026N", "auravance.aura")

# ───────────────────────── text styles ─────────────────────────
BODY = ParagraphStyle("body", fontName="Helvetica", fontSize=7.8, leading=9.6, spaceAfter=2.6)
SMALL = ParagraphStyle("small", parent=BODY, fontSize=6.8, leading=8.2, spaceAfter=1.5)
CELL = ParagraphStyle("cell", fontName="Helvetica", fontSize=6.6, leading=7.8)
CELLB = ParagraphStyle("cellb", parent=CELL, fontName="Helvetica-Bold")
CELLR = ParagraphStyle("cellr", parent=CELL, alignment=2)
TITLE = ParagraphStyle("title", fontName="Helvetica-Bold", fontSize=13.5, leading=16, textColor=NAVY, spaceAfter=1)
SUB = ParagraphStyle("sub", fontName="Helvetica", fontSize=8.2, leading=10, textColor=TEAL, spaceAfter=4)
H2 = ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=9.2, leading=11, textColor=NAVY, spaceBefore=4, spaceAfter=2)
CAPTION = ParagraphStyle("cap", parent=SMALL, alignment=TA_CENTER, textColor=colors.HexColor("#444444"))


def P(text: str, style=BODY) -> Paragraph:
    return Paragraph(text, style)


def H(text: str) -> Paragraph:
    return Paragraph(text, H2)


# ───────────────────────── number helpers ─────────────────────────
def ind(n: float, dec: int = 0) -> str:
    """Indian digit grouping: 4,50,00,00,000 / 12,34,567.50"""
    s = f"{abs(n):.{dec}f}"
    whole, _, frac = s.partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts + [tail])
    out = whole + ("." + frac if dec else "")
    return ("-" if n < 0 else "") + out


def cr(x: float, dec: int = 2) -> str:
    return f"{x:,.{dec}f}"


# ───────────────────────── table helpers ─────────────────────────
def grid_table(data, col_widths, header_rows=1, zebra=True, right_cols=()):
    """Ruled table (real vector grid lines so PyMuPDF find_tables detects it)."""
    rows = []
    for ri, row in enumerate(data):
        cells = []
        for ci, c in enumerate(row):
            if isinstance(c, str):
                style = CELLB if ri < header_rows else (CELLR if ci in right_cols else CELL)
                c = Paragraph(c, style)
            cells.append(c)
        rows.append(cells)
    t = Table(rows, colWidths=col_widths, repeatRows=header_rows)
    style = [
        ("GRID", (0, 0), (-1, -1), 0.5, GRID),
        ("BACKGROUND", (0, 0), (-1, header_rows - 1), KEYBG),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 1.6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1.6),
        ("LEFTPADDING", (0, 0), (-1, -1), 2.6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2.6),
    ]
    if zebra:
        for r in range(header_rows, len(rows)):
            if (r - header_rows) % 2 == 1:
                style.append(("BACKGROUND", (0, r), (-1, r), BAND))
    t.setStyle(TableStyle(style))
    return t


def kv_table(pairs, key_w=48 * mm):
    rows = [[Paragraph(k, CELLB), Paragraph(v, CELL)] for k, v in pairs]
    t = Table(rows, colWidths=[key_w, FRAME_W - key_w])
    t.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, GRID),
        ("BACKGROUND", (0, 0), (0, -1), KEYBG),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    return t


def signoff_table(rows):
    data = [["Role", "Name", "Designation", "Date", "Signature"]]
    data += [[r[0], r[1], r[2], r[3], "______________________"] for r in rows]
    return grid_table(data, [34 * mm, 38 * mm, 42 * mm, 24 * mm, FRAME_W - 138 * mm], zebra=False)


def escalation_table(rows):
    data = [["Level", "Who to contact", "How to reach", "Resolution timeline"]] + rows
    return grid_table(data, [20 * mm, 52 * mm, 62 * mm, FRAME_W - 134 * mm])


# ───────────────────────── chart helpers (Pillow → PNG) ─────────────────────────
PALETTE = [(20, 40, 75), (31, 111, 120), (214, 122, 36), (120, 130, 150), (86, 148, 82), (140, 80, 150), (190, 60, 60)]


def _font(size: int, bold: bool = False):
    for name in (("arialbd.ttf" if bold else "arial.ttf"), "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def line_chart(title, labels, values, y_label, x_label, note, unit="%", y_min=0.0, y_max=None):
    W, Hh = 1500, 600
    img = Image.new("RGB", (W, Hh), "white")
    d = ImageDraw.Draw(img)
    l, r, t, b = 120, 50, 85, 120
    y_max = y_max or max(values) * 1.15
    d.text((W // 2, 30), title, font=_font(34, True), fill=PALETTE[0], anchor="mm")
    for i in range(6):
        yv = y_min + (y_max - y_min) * i / 5
        y = Hh - b - (Hh - t - b) * i / 5
        d.line([(l, y), (W - r, y)], fill=(215, 220, 230), width=2)
        d.text((l - 14, y), f"{yv:.0f}{unit}", font=_font(24), fill=(60, 60, 60), anchor="rm")
    d.line([(l, t), (l, Hh - b)], fill=(40, 40, 40), width=3)
    d.line([(l, Hh - b), (W - r, Hh - b)], fill=(40, 40, 40), width=3)
    n = len(values)
    pts = []
    for i, v in enumerate(values):
        x = l + (W - l - r) * (i + 0.5) / n
        y = Hh - b - (Hh - t - b) * (v - y_min) / (y_max - y_min)
        pts.append((x, y))
        d.text((x, Hh - b + 22), labels[i], font=_font(24), fill=(40, 40, 40), anchor="mm")
    d.line(pts, fill=PALETTE[2], width=7, joint="curve")
    for (x, y), v in zip(pts, values):
        d.ellipse([x - 10, y - 10, x + 10, y + 10], fill=PALETTE[0], outline="white", width=3)
        d.text((x, y - 28), f"{v:.1f}{unit}", font=_font(25, True), fill=PALETTE[0], anchor="mm")
    d.text((W // 2, Hh - b + 62), x_label, font=_font(26), fill=(40, 40, 40), anchor="mm")
    ylab = Image.new("RGBA", (Hh, 40), (255, 255, 255, 0))
    ImageDraw.Draw(ylab).text((Hh // 2, 20), y_label, font=_font(26), fill=(40, 40, 40), anchor="mm")
    rot = ylab.rotate(90, expand=True)
    img.paste(rot, (8, 0), rot)
    d.text((W // 2, Hh - 14), note, font=_font(21), fill=(110, 110, 110), anchor="mm")
    return _png(img), W, Hh


def pie_chart(title, labels, values, note):
    W, Hh = 1500, 600
    img = Image.new("RGB", (W, Hh), "white")
    d = ImageDraw.Draw(img)
    d.text((W // 2, 30), title, font=_font(34, True), fill=PALETTE[0], anchor="mm")
    total = sum(values)
    box = [90, 80, 90 + 440, 80 + 440]
    start = -90.0
    for i, v in enumerate(values):
        sweep = 360.0 * v / total
        d.pieslice(box, start, start + sweep, fill=PALETTE[i % len(PALETTE)], outline="white", width=4)
        start += sweep
    for i, (lab, v) in enumerate(zip(labels, values)):
        y = 120 + i * 62
        d.rectangle([640, y, 676, y + 36], fill=PALETTE[i % len(PALETTE)])
        d.text((696, y + 18), f"{lab}", font=_font(27), fill=(30, 30, 30), anchor="lm")
        d.text((W - 60, y + 18), f"{100 * v / total:.2f}%", font=_font(27, True), fill=PALETTE[0], anchor="rm")
    d.text((W // 2, Hh - 22), note, font=_font(21), fill=(110, 110, 110), anchor="mm")
    return _png(img), W, Hh


def hbar_chart(title, labels, values, x_label, note, unit="%"):
    W, Hh = 1500, 600
    img = Image.new("RGB", (W, Hh), "white")
    d = ImageDraw.Draw(img)
    d.text((W // 2, 30), title, font=_font(34, True), fill=PALETTE[0], anchor="mm")
    l, r, t, b = 470, 120, 80, 105
    vmax = max(values) * 1.15
    n = len(values)
    slot = (Hh - t - b) / n
    for i in range(6):
        x = l + (W - l - r) * i / 5
        d.line([(x, t), (x, Hh - b)], fill=(215, 220, 230), width=2)
        d.text((x, Hh - b + 22), f"{vmax * i / 5:.2f}", font=_font(23), fill=(60, 60, 60), anchor="mm")
    for i, (lab, v) in enumerate(zip(labels, values)):
        y0 = t + slot * i + slot * 0.18
        y1 = t + slot * (i + 1) - slot * 0.18
        x1 = l + (W - l - r) * v / vmax
        d.rectangle([l, y0, x1, y1], fill=PALETTE[i % len(PALETTE)])
        d.text((l - 16, (y0 + y1) / 2), lab, font=_font(26), fill=(30, 30, 30), anchor="rm")
        d.text((x1 + 12, (y0 + y1) / 2), f"{v:.2f}{unit}", font=_font(26, True), fill=PALETTE[0], anchor="lm")
    d.line([(l, t), (l, Hh - b)], fill=(40, 40, 40), width=3)
    d.text(((l + W - r) // 2, Hh - b + 60), x_label, font=_font(26), fill=(40, 40, 40), anchor="mm")
    d.text((W // 2, Hh - 16), note, font=_font(21), fill=(110, 110, 110), anchor="mm")
    return _png(img), W, Hh


def chart_flowable(chart, width_pt=FRAME_W):
    png, w, h = chart
    return RLImage(io.BytesIO(png), width=width_pt, height=width_pt * h / w)


# ───────────────────────── document scaffolding ─────────────────────────
def build_pdf(path: Path, doc_code: str, banner: str, classification: str, pages: list[list]):
    assert len(pages) == 3, "each document is exactly three pages"

    def on_page(canvas, doc):
        canvas.saveState()
        canvas.setFillColor(NAVY)
        canvas.rect(0, PAGE_H - 15 * mm, PAGE_W, 15 * mm, stroke=0, fill=1)
        canvas.setFillColor(colors.white)
        canvas.setFont("Helvetica-Bold", 8.5)
        canvas.drawString(MARGIN_X, PAGE_H - 9.4 * mm, banner)
        canvas.setFont("Helvetica", 8)
        canvas.drawRightString(PAGE_W - MARGIN_X, PAGE_H - 9.4 * mm, classification.upper())
        canvas.setStrokeColor(GRID)
        canvas.line(MARGIN_X, 14 * mm, PAGE_W - MARGIN_X, 14 * mm)
        canvas.setFillColor(colors.HexColor("#333333"))
        canvas.setFont("Helvetica", 6.8)
        canvas.drawString(MARGIN_X, 10.2 * mm, f"Document Identification Code: {doc_code}   |   Classification: {classification}")
        canvas.drawRightString(PAGE_W - MARGIN_X, 10.2 * mm, f"Page {doc.page} of 3")
        canvas.setFont("Helvetica-Oblique", 6.2)
        canvas.drawString(MARGIN_X, 6.9 * mm, f"{CO.legal} | CIN {CO.cin} | PAN {CO.pan}")
        canvas.drawString(MARGIN_X, 3.7 * mm, "Sample enterprise document prepared for RAG deployment. Figures are illustrative; replace with approved company data before production use.")
        canvas.restoreState()

    def make(mode: str):
        doc = BaseDocTemplate(str(path), pagesize=A4, leftMargin=MARGIN_X, rightMargin=MARGIN_X,
                              topMargin=TOP, bottomMargin=BOTTOM, title=banner, author=CO.legal)
        frame = Frame(MARGIN_X, BOTTOM, FRAME_W, FRAME_H, leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
        doc.addPageTemplates([PageTemplate(id="p", frames=[frame], onPage=on_page)])
        story = []
        for i, content in enumerate(pages):
            story.append(KeepInFrame(FRAME_W, FRAME_H - 1, list(content), mode=mode))
            if i < 2:
                story.append(PageBreak())
        doc.build(story)

    try:
        make("error")          # content must fit each page at full size
    except LayoutError:
        print(f"   WARNING {path.name}: a page overflowed; shrinking to keep exactly 3 pages (trim text to fix)")
        make("shrink")


# ═════════════════════════ DOCUMENT 1 — BANKING PILLAR (NBFC lending) ═════════════════════════
def identity_line() -> str:
    return f"{CO.legal} (CIN {CO.cin}; PAN {CO.pan}), registered office: {CO.house}"


def doc_banking() -> tuple[str, list[list]]:
    code = f"{CO.ini}-BNK-RTL-2026-001"
    me = CO.short

    grades = [  # grade, PD band, spread %, fee %, security cover x, authority
        ("CR1", "Up to 0.50%", 1.10, 0.25, 1.00, "Zonal Credit Head"),
        ("CR2", "0.50% to 1.00%", 1.45, 0.30, 1.10, "Zonal Credit Head"),
        ("CR3", "1.00% to 2.00%", 1.85, 0.40, 1.25, "Chief Credit Officer"),
        ("CR4", "2.00% to 4.00%", 2.40, 0.50, 1.40, "Chief Credit Officer"),
        ("CR5", "4.00% to 8.00%", 3.10, 0.60, 1.50, "Credit Committee of the Board"),
        ("CR6", "Above 8.00%", 3.90, 0.75, 1.75, "Credit Committee of the Board with full Board ratification"),
    ]
    t_price = [["Internal Rating Grade", "Indicative One-Year PD Band", "Spread over ABLR (% p.a.)", "Processing Fee (% of sanction)", "Min. Security Cover (times)", "Sanctioning Authority"]]
    t_price += [[g, pd, f"{s:.2f}", f"{f:.2f}", f"{c:.2f}", a] for g, pd, s, f, c, a in grades]

    variants = [
        ["Variant", "Purpose", "Ticket Size (INR cr)", "Max Tenor", "Moratorium", "Repayment"],
        ["Business Term Loan (Capex)", "Machinery, equipment and expansion for MSMEs and mid-size businesses", "0.25 to 10", "7 years", "Up to 12 months", "Monthly or quarterly"],
        ["Equipment Finance Term Loan", "Purchase of specific equipment, which is hypothecated to the Company", "0.10 to 5", "5 years", "Up to 6 months", "Monthly"],
        ["Working Capital Term Loan", "Funding of permanent working capital", "0.10 to 5", "4 years", "Up to 6 months", "Monthly or quarterly"],
        ["Consortium / Co-lending Facility", "Where the Company lends together with one or more banks", "Above 10", "As agreed", "As agreed", "Quarterly"],
    ]

    p1 = [
        P(f"{me.upper()}: RUPEE TERM LOAN / CREDIT FACILITY AGREEMENT (STANDARD FORM)", TITLE),
        P(f"Standard Facility Agreement and Customer Terms No. {code} &nbsp;|&nbsp; Effective 01 October 2026 &nbsp;|&nbsp; Next review 30 September 2027", SUB),
        H("Part A. Enterprise Metadata and Data Classification"),
        kv_table([
            ("Industry Pillar", "Banking (lending, as a Non-Banking Financial Company)"),
            ("Target Enterprise Document Type", "Rupee Term Loan (RTL) / Credit Facility Agreement, standard form with co-lending and consortium schedule"),
            ("Regulatory Framework", "Reserve Bank of India Act, 1934 (Sections 45JA and 45L directions to NBFCs); RBI Master Direction on NBFCs - Scale Based Regulation (income recognition, asset classification and provisioning); RBI Digital Lending Directions (Key Fact Statement, cooling-off, grievance redressal); RBI Fair Practices Code and directions on penal charges and prepayment; SARFAESI Act, 2002, to the extent the Company is a secured creditor under it"),
            ("Document Identification Code", code),
            ("Classification Level", "Strictly Confidential"),
            ("Issuing Entity", f"{identity_line()}; RBI Certificate of Registration as an NBFC No. {CO.nbfc_cor} (illustrative)"),
            ("Document Owner and Audience", "Lending Products and Credit Policy. Used by relationship managers, credit officers, collections, compliance and legal teams; the Customer Terms extract (Parts B, C and E) is issued to every sanctioned borrower"),
            ("Governing Law and Forum", "Laws of India; courts at Mumbai and, where applicable, the Debts Recovery Tribunal, Mumbai"),
        ]),
        Spacer(1, 3),
        H("Part B. Applicability and Facility Variants"),
        P(f"This standard form applies to every business Rupee Term Loan sanctioned by {CO.legal} (the Company), whether it lends alone, co-lends with a bank, or participates in a consortium. Each sanction letter names the Borrower, the facility variant, the internal rating grade and the pricing; where a sanction letter conflicts with this form, the sanction letter prevails."),
        grid_table(variants, [38 * mm, 56 * mm, 24 * mm, 20 * mm, 22 * mm, FRAME_W - 160 * mm]),
        Spacer(1, 3),
        H("Part C. Preliminary Operational Framework and Pricing"),
        P(f"<b>1. Interest and Benchmark Tracking.</b> Interest is floating and linked to the {me} Benchmark Lending Rate (ABLR), the Company's internal benchmark published on www.{CO.dom} on the first working day of each month. The applicable rate is the ABLR plus the spread for the Borrower's rating grade (Table 1). The rate resets every twelve months on the anniversary of first disbursement; the spread changes only on a documented credit-risk review communicated in writing. Interest is calculated on the daily outstanding on an actual/365 basis."),
        P("<b>2. Key Fact Statement and Cooling-Off.</b> Before the agreement is executed the Borrower receives a Key Fact Statement (KFS) showing the annualised percentage rate (APR), every fee and charge, the repayment schedule and the grievance officer's contact details. The Borrower has a cooling-off period of 3 days from disbursement to exit by repaying the principal and the proportionate APR, with no penalty. Loans are disbursed to, and repaid from, the Borrower's own bank account directly; a lending service provider acts only as an agent and never holds the funds."),
        P("<b>3. Security and Hypothecation.</b> The Company takes a first-ranking exclusive charge by hypothecation over the assets financed, a first pari passu charge over current assets, and where the sanction requires an equitable mortgage over immovable property. The minimum security cover depends on the rating grade (Table 1) and is tested at each half-year end."),
        P("<b>4. Co-Lending and Consortium.</b> Where a facility is co-lent or syndicated with a bank, the bank's share is priced under that bank's own benchmark (its MCLR or external benchmark) and the Borrower is told the blended rate in the KFS. The Facility Agent named in the sanction holds the security as Security Trustee, shared pro rata to commitments; decisions reserved for the Majority Lenders require lenders holding at least 66.67 per cent of commitments."),
        P("<b>5. Penal Charges and Prepayment.</b> Non-compliance with material terms attracts penal charges of 2.00 per cent per annum on the overdue amount only; penal charges are not capitalised and carry no further interest. Prepayment of a floating-rate loan to an individual or a micro or small enterprise borrower carries no prepayment charge; for other borrowers, prepayment from internal accruals is free and prepayment funded by refinancing from another lender attracts a premium of 1.00 per cent of the principal prepaid."),
        P("Table 1. Pricing Grid by Internal Credit Rating Grade (illustrative, effective 01 October 2026; reviewed by the Asset Liability Committee).", SMALL),
        grid_table(t_price, [20 * mm, 30 * mm, 30 * mm, 28 * mm, 26 * mm, FRAME_W - 134 * mm], right_cols=(2, 3, 4)),
    ]

    # ---- page 2 : NBFC provisioning (standard 0.40, sub-standard 10, doubtful secured 20/30/50 + unsecured 100, loss 100)
    prov = [
        ("Standard asset", "0 days past due", 70, 30, 0.40, 0.40),
        ("SMA-0 (special mention)", "1 to 30 days", 70, 30, 0.40, 0.40),
        ("SMA-1 (special mention)", "31 to 60 days", 70, 30, 0.40, 0.40),
        ("SMA-2 (special mention)", "61 to 90 days", 70, 30, 0.40, 0.40),
        ("Sub-standard (NPA)", "More than 90 days, NPA for up to 12 months", 70, 30, 10.0, 10.0),
        ("Doubtful D1", "Doubtful for up to 1 year", 70, 30, 20.0, 100.0),
        ("Doubtful D2", "Doubtful for 1 to 3 years", 70, 30, 30.0, 100.0),
        ("Doubtful D3", "Doubtful for more than 3 years", 70, 30, 50.0, 100.0),
        ("Loss asset", "Identified as uncollectible", 70, 30, 100.0, 100.0),
    ]
    t_a = [["Asset Category", "Default / Ageing Window", "Secured Portion (INR cr)", "Unsecured Portion (INR cr)", "Secured Prov. (%)", "Unsecured Prov. (%)", "Provision (INR cr)", "Effective Prov. (%)"]]
    for name, win, sec, uns, ps, pu in prov:
        provision = sec * ps / 100 + uns * pu / 100
        t_a.append([name, win, cr(sec), cr(uns), f"{ps:.2f}", f"{pu:.2f}", cr(provision), f"{provision / (sec + uns) * 100:.2f}"])

    # worked example: INR 5 cr, grade CR3, 12-month moratorium, 20 quarterly instalments of INR 0.25 cr
    sanction, spread = 5.0, 1.85
    inst = sanction / 20
    ablr_path = [11.40] * 4 + [11.15] * 4
    qends = [date(2026, 12, 31), date(2027, 3, 31), date(2027, 6, 30), date(2027, 9, 30), date(2027, 12, 31), date(2028, 3, 31), date(2028, 6, 30), date(2028, 9, 30)]
    t_b = [["Quarter Ended", "Opening Outstanding (INR cr)", "Principal Repaid (INR cr)", "Closing Outstanding (INR cr)", "ABLR (%)", "Spread (%)", "Applicable Rate (%)", "Interest (INR cr)", "Interest (INR)"]]
    out = sanction
    total_int = total_rep = 0.0
    for i, (qe, m) in enumerate(zip(qends, ablr_path)):
        opening = out
        repaid = inst if i >= 4 else 0.0
        out = opening - repaid
        rate = m + spread
        interest = opening * rate / 100 / 4
        total_int += interest
        total_rep += repaid
        t_b.append([qe.strftime("%d %b %Y"), cr(opening), cr(repaid), cr(out), f"{m:.2f}", f"{spread:.2f}", f"{rate:.2f}", cr(interest, 4), ind(interest * 1e7)])
    t_b.append(["Eight-quarter total", "-", cr(total_rep), cr(out), "-", "-", "-", cr(total_int, 4), ind(total_int * 1e7)])

    chart = line_chart(
        "Scheduled Commercial Banks: Gross NPA (GNPA) Ratio Trend",
        ["FY2018", "FY2019", "FY2020", "FY2021", "FY2022", "FY2023", "FY2024", "FY2025"],
        [11.2, 9.1, 8.2, 7.3, 5.9, 3.9, 2.8, 2.3], "GNPA ratio (% of gross advances)", "Fiscal year ended 31 March",
        "Approximate year-end values compiled from RBI Financial Stability Report editions; verify against the latest edition.",
    )
    p2 = [
        P("Part D. High-Density Data Grids", H2),
        P(f"Table 2. Asset Classification and Provisioning Matrix used by the Company's credit risk team (illustrative INR 100 crore exposure, 70 per cent realisable security cover). It tracks repayment default windows against secured and unsecured provisioning percentages, following the RBI NBFC norms: 0.40 per cent on standard assets, 10 per cent on sub-standard assets, 100 per cent of the unsecured portion plus 20, 30 or 50 per cent of the secured portion on doubtful assets by age, and 100 per cent on loss assets. Where the Company applies Ind AS, it recognises expected credit loss and holds any shortfall against these regulatory floors in an impairment reserve.", SMALL),
        grid_table(t_a, [30 * mm, 38 * mm, 21 * mm, 22 * mm, 16 * mm, 17 * mm, 19 * mm, FRAME_W - 163 * mm], right_cols=(2, 3, 4, 5, 6, 7)),
        Spacer(1, 4),
        P(f"Table 3. Worked Example: Business Term Loan of INR 5 crore, rating grade CR3 (spread 1.85 per cent), disbursed in full on 01 October 2026, 12-month moratorium, then 20 equal quarterly instalments of INR 0.25 crore from 31 December 2027. Interest is charged on the opening outstanding of each quarter at the applicable rate divided by four. The ABLR resets from 11.40 to 11.15 per cent on 01 October 2027.", SMALL),
        grid_table(t_b, [22 * mm, 22 * mm, 20 * mm, 22 * mm, 13 * mm, 13 * mm, 17 * mm, 21 * mm, FRAME_W - 150 * mm], right_cols=(1, 2, 3, 4, 5, 6, 7, 8)),
        Spacer(1, 4),
        P("Figure 1. Line chart: Scheduled Commercial Banks Gross NPA ratio, FY2018 to FY2025. X-axis: fiscal year. Y-axis: GNPA ratio in per cent of gross advances. The line falls from 11.2 per cent in FY2018 to 2.3 per cent in FY2025. The Company uses this banking-sector series, the asset quality of its co-lending and consortium partners, as the baseline when calibrating covenants and the pricing grid.", SMALL),
        chart_flowable(chart, FRAME_W * 0.86),
        P("Figure 1 caption: Asset quality (GNPA) trend of Scheduled Commercial Banks across eight fiscal years.", CAPTION),
    ]

    p3 = [
        P("Part E. Statutory Covenants, Regulatory Risk Framework and Audit Sign-Off", H2),
        P("<b>6. Financial Covenants.</b> Throughout the tenor the Borrower shall maintain a Debt Service Coverage Ratio of at least 1.25 times (tested annually on audited accounts), Total Outside Liabilities to Tangible Net Worth of at most 3.00 times, a Current Ratio of at least 1.20 times and the minimum security cover of its rating grade. A breach not remedied within thirty days of written notice is an Event of Default."),
        P("<b>7. Negative Covenants and Reporting.</b> Without prior written consent the Borrower shall not create a further charge over the financed assets, change control, declare dividends while any dues are overdue, or divert funds from the stated purpose. The Borrower furnishes quarterly results within 45 days, audited accounts within 180 days and a monthly stock and book-debt statement within 15 days. The Company reports the account to the credit information companies as required and tracks Special Mention Account categories (SMA-0, SMA-1, SMA-2) for early warning."),
        P("<b>8. Asset Classification.</b> The Company classifies the facility under the RBI norms applicable to NBFCs. An account becomes a Non-Performing Asset when interest or an instalment of principal remains overdue for more than 90 days; income on an NPA is not recognised on an accrual basis. In a co-lending or consortium arrangement each lender classifies independently, and the more conservative classification is disclosed to the Facility Agent. Resolution of stressed assets follows the RBI framework, including the review period and, where required, an Inter-Creditor Agreement approved by lenders representing 75 per cent by value and 60 per cent by number."),
        P("<b>9. Events of Default.</b> (a) Non-payment of principal or interest within three business days of the due date; (b) breach of financial or negative covenants; (c) misrepresentation; (d) cross-default on other borrowings above INR 1 crore; (e) insolvency, winding up or proceedings under the Insolvency and Bankruptcy Code, 2016; (f) cessation of business or loss of material licences; (g) fall in security cover below the minimum without top-up within thirty days."),
        P("<b>10. SARFAESI Act, 2002: Statutory Disclosures.</b> Where the Company is a secured creditor under the Act, it may enforce security interest without the intervention of a court once the account is classified as a Non-Performing Asset. As a matter of its own policy the Company first issues a reminder and offers a meeting. (a) Under Section 13(2), a written demand notice calls upon the Borrower to discharge liabilities in full within sixty days; (b) on failure, the Company may exercise the measures in Section 13(4), including taking possession of secured assets, taking over management, appointing a manager, or requiring the Borrower's receivables to be paid to the Company; (c) where possession is resisted, the Chief Metropolitan Magistrate or District Magistrate assists under Section 14; (d) the Borrower's right to redeem the asset by tendering all dues ends on publication of the notice of sale, in line with Section 13(8); (e) a Borrower aggrieved by any measure may apply to the Debts Recovery Tribunal under Section 17 within forty-five days of the measure."),
        P("<b>11. Statutory Overlay and Disputes.</b> This Agreement is subject to the Reserve Bank of India Act, 1934 and to every direction and circular of the Reserve Bank of India applicable to NBFCs. In case of conflict the binding direction prevails and the clause is read down to that extent. Disputes are first referred to senior management of the Borrower and the Company within fifteen days, then to arbitration under the Arbitration and Conciliation Act, 1996, seated in Mumbai, without prejudice to the Company's statutory remedies."),
        H("Customer Grievance Redressal and Escalation Matrix"),
        escalation_table([
            ["Level 1", "Relationship Manager or Customer Care", f"App or email to your RM, or customer care {CO.lend_tf} (24x7); care@{CO.dom}", "Acknowledged in 1 working day; resolved within 7 working days"],
            ["Level 2", "Grievance Redressal Officer (digital lending nodal officer): Rekha Balasubramanian", f"grievance@{CO.dom}", "Resolved within 10 working days"],
            ["Level 3", "Principal Nodal Officer: Vivek Anand Rastogi", f"pno@{CO.dom}; {CO.house}", "Final reply within 30 days of the complaint"],
            ["Level 4", "RBI Ombudsman (Reserve Bank - Integrated Ombudsman Scheme)", "Online at the RBI complaint management portal (cms.rbi.org.in)", "If unresolved within 30 days or the reply is unsatisfactory"],
        ]),
        Spacer(1, 3),
        H("Audit and Approval Sign-Off"),
        signoff_table([
            ("Head, Lending Products", "Rajiv Narayanan Pillai", "Senior Vice President", "30 Sep 2026"),
            ("Chief Credit Officer", "Farida Qureshi Hashmi", "Chief Credit Officer", "30 Sep 2026"),
            ("Chief Compliance Officer", "Anand Subramaniam Rao", "Chief Compliance Officer", "30 Sep 2026"),
            ("Legal Counsel", "Kavita Deshmukh Joshi", "Head, Legal", "30 Sep 2026"),
            ("Internal Audit Review", "Sanjay Kulkarni and Associates", "Chartered Accountants, Partner", "01 Oct 2026"),
        ]),
    ]
    return code, [p1, p2, p3]


# ═════════════════════════ DOCUMENT 2 — FINANCIAL SERVICES (MF distribution) ═════════════════════════
def doc_finserv() -> tuple[str, list[list]]:
    code = f"{CO.ini}-MF-ECAS-2026-0392"
    me = CO.short
    # (scheme, category, folio, units, nav, cost_value) — third-party AMCs (fictional), Regular Plans
    holdings = [
        ("Bharat Horizon Flexi Cap Fund - Regular Plan - Growth", "Equity", "14027365/41", 4520.318, 187.4260, 694_000),
        ("Sindhu Capital Large Cap Fund - Regular Plan - Growth", "Equity", "88213907/12", 3180.742, 142.8115, 395_000),
        ("Vikram Wealth ELSS Tax Saver Fund - Regular Plan - Growth", "Equity (ELSS)", "55120874/03", 2210.505, 96.3340, 182_000),
        ("Vikram Wealth Aggressive Hybrid Fund - Regular Plan - Growth", "Hybrid", "55120874/07", 1860.226, 121.0575, 201_000),
        ("Sindhu Capital Corporate Bond Fund - Regular Plan - Growth", "Debt", "88213907/19", 9120.884, 34.2290, 292_000),
        ("Bharat Horizon Liquid Fund - Regular Plan - Growth", "Liquid", "14027365/58", 612.340, 1_428.7710, 841_000),
    ]
    rows_val = []
    tot_mv = tot_cost = 0.0
    by_cat: dict[str, float] = {}
    for sch, cat, folio, units, nav, cost in holdings:
        mv = units * nav
        tot_mv += mv
        tot_cost += cost
        key = cat.split(" (")[0]
        by_cat[key] = by_cat.get(key, 0.0) + mv
        rows_val.append([sch, folio, f"{units:,.3f}", f"{nav:,.4f}", ind(cost), ind(mv, 2), ind(mv - cost, 2), f"{(mv - cost) / cost * 100:.2f}"])
    t_val = [["Scheme Name", "Folio Number", "Available Units", "Live NAV (INR) 30-Sep-2026", "Cost Value (INR)", "Current Market Value (INR)", "Unrealised Gain (INR)", "Gain (%)"]] + rows_val
    t_val.append(["Portfolio total", "-", "-", "-", ind(tot_cost), ind(tot_mv, 2), ind(tot_mv - tot_cost, 2), f"{(tot_mv - tot_cost) / tot_cost * 100:.2f}"])

    reds = [
        ("Bharat Horizon Flexi Cap Fund", "Equity", date(2023, 5, 18), 118.9410, date(2026, 6, 14), 187.0200, 2000.000),
        ("Sindhu Capital Large Cap Fund", "Equity", date(2025, 12, 11), 131.5500, date(2026, 8, 20), 141.9000, 800.000),
        ("Sindhu Capital Corporate Bond Fund", "Debt", date(2023, 6, 9), 29.1000, date(2026, 7, 25), 33.9800, 6000.000),
        ("Bharat Horizon Liquid Fund", "Liquid", date(2026, 3, 3), 1_391.2200, date(2026, 9, 5), 1_423.9500, 150.000),
    ]
    SLAB = 0.30
    t_cg = [["Scheme", "Type", "Purchase Date", "Purchase NAV", "Redemption Date", "Redemption NAV", "Units", "Holding (days)", "Gain Class", "Gain (INR)"]]
    ltcg_total = stcg_eq = debt_gain = 0.0
    for sch, typ, pd_, pn, rd, rn, u in reds:
        days = (rd - pd_).days
        gain = (rn - pn) * u
        if typ == "Equity":
            cls = "LTCG" if days > 365 else "STCG"
            if cls == "LTCG":
                ltcg_total += gain
            else:
                stcg_eq += gain
        else:
            cls = "STCG (slab)"
            debt_gain += gain
        t_cg.append([sch, typ, pd_.strftime("%d-%b-%Y"), f"{pn:,.4f}", rd.strftime("%d-%b-%Y"), f"{rn:,.4f}", f"{u:,.3f}", str(days), cls, ind(gain, 2)])
    exemption = 125000
    ltcg_taxable = max(ltcg_total - exemption, 0)
    tax_ltcg, tax_stcg, tax_debt = ltcg_taxable * 0.125, stcg_eq * 0.20, debt_gain * SLAB
    t_tax = [
        ["Capital Gains Tax Distribution, FY 2026-27 (April to September)", "Gain (INR)", "Exemption / Basis", "Taxable (INR)", "Rate", "Indicative Tax (INR)"],
        ["Long-Term Capital Gains (LTCG), equity-oriented, held more than 12 months", ind(ltcg_total, 2), "Exemption INR 1,25,000 per FY", ind(ltcg_taxable, 2), "12.5%", ind(tax_ltcg, 2)],
        ["Short-Term Capital Gains (STCG), equity-oriented, held up to 12 months", ind(stcg_eq, 2), "None", ind(stcg_eq, 2), "20%", ind(tax_stcg, 2)],
        ["Debt and liquid schemes (treated as short-term, taxed at slab rate, illustrative 30%)", ind(debt_gain, 2), "Section 50AA style deemed STCG", ind(debt_gain, 2), "30%", ind(tax_debt, 2)],
        ["Total indicative tax before surcharge and 4% cess", ind(ltcg_total + stcg_eq + debt_gain, 2), "-", ind(ltcg_taxable + stcg_eq + debt_gain, 2), "-", ind(tax_ltcg + tax_stcg + tax_debt, 2)],
    ]
    alloc_labels = list(by_cat.keys())
    pie = pie_chart(f"{me} Platform: Portfolio Asset Allocation by Category", alloc_labels, [by_cat[k] for k in alloc_labels],
                    f"Total market value INR {ind(tot_mv, 2)} as on 30 September 2026. Sample investor portfolio.")

    svc = [
        ["Service", "Regulatory or AMC Position", f"{me} Service Commitment"],
        ["Order placement and NAV", "NAV depends on the time of receipt at the AMC; cut-offs are in each Scheme Information Document", "Order sent to the AMC within 15 minutes of submission before the cut-off; confirmation in the app and by email"],
        ["Redemption proceeds", "Paid by the AMC within 3 working days (Liquid Fund: 1 working day)", "Tracks every redemption and escalates to the AMC after the second working day"],
        ["Statement availability", "Depository or RTA issues the CAS by the 7th day after a transaction month", "Statement visible on the platform within 2 working days of receipt"],
        ["Change of nomination, bank mandate or contact details", "Processed by the AMC or RTA", "Request lodged with the RTA within 1 working day of a complete request"],
        ["Investor complaint", "Complaints against an AMC or RTA go to SEBI SCORES", "Acknowledged in 1 working day; resolved within 10 working days"],
    ]

    p1 = [
        P(f"{me.upper()}: MUTUAL FUND CONSOLIDATED ACCOUNT STATEMENT (eCAS) AND INVESTOR SERVICING GUIDE", TITLE),
        P(f"Statement period 01 April 2026 to 30 September 2026 &nbsp;|&nbsp; Statement No. {code} &nbsp;|&nbsp; Issued 03 October 2026 by electronic mail", SUB),
        H("Part A. Enterprise Metadata and Data Classification"),
        kv_table([
            ("Industry Pillar", "Financial Services (mutual fund distribution and investor servicing)"),
            ("Target Enterprise Document Type", "Mutual Fund Consolidated Account Statement (eCAS) with investor servicing guide"),
            ("Regulatory Framework", "SEBI Master Circular for Mutual Funds; SEBI directions on Consolidated Account Statements issued by depositories and Registrar and Transfer Agents; AMFI Code of Conduct for mutual fund distributors; Income-tax Act, 1961 (capital gains provisions)"),
            ("Document Identification Code", code),
            ("Classification Level", "Strictly Confidential"),
            ("Presenting Entity", f"{identity_line()}; AMFI-registered Mutual Fund Distributor {CO.arn} (illustrative). The statutory CAS is issued by the depositories and Registrar and Transfer Agents; the Company presents that data on its platform"),
            ("Dispatch Channel and Frequency", "Email and platform; monthly where a transaction occurred in the month, half-yearly (September and March) otherwise"),
        ]),
        Spacer(1, 3),
        H("Part B. Client Account Distribution Mapping and Identity Validation"),
        grid_table([
            ["Field", "Value", "Validation Status"],
            ["Investor name", "Meenakshi Raghavan Iyer", "Matched with KYC register"],
            ["PAN (masked identity)", "XXXXXX482K", "Valid, PAN-Aadhaar linked"],
            ["Registered mobile", "+91-XXXXXX4172", "Verified by one-time password"],
            ["Registered email", f"m.r****@{CO.dom}", "Verified"],
            ["Tax status", "Resident Individual", "Declared in folio records"],
            ["KYC status", "KYC Validated (registered with a KYC Registration Agency)", "Active"],
            ["Nomination status", "Nominee registered in 5 of 6 folios", "One folio (14027365/58) pending nomination opt-in or opt-out"],
            ["Distribution mapping", f"6 folios across 3 Asset Management Companies, all Regular Plans, mapped to distributor {CO.arn} ({me})", "Consistent across folios"],
        ], [38 * mm, 84 * mm, FRAME_W - 122 * mm]),
        Spacer(1, 3),
        H("Part C. Preliminary Operational Framework and Service Commitments"),
        P("<b>1. What a Consolidated Account Statement is.</b> A single statement that consolidates every mutual fund folio and demat holding of an investor, combined on the basis of the PAN and the order of holders. Where units are held in demat mode, the depository issues it; otherwise the Registrar and Transfer Agent does. It shows each transaction in the period, opening and closing unit balances, the applicable NAV and the market value at the period end. A monthly statement is sent for any month with a financial transaction; otherwise a statement is sent half-yearly, in September and March."),
        P("<b>2. Valuation and Masking.</b> Units are valued at the closing NAV of the scheme on the last business day of the period. Cost value is the weighted-average acquisition cost and is for information only; it is not a substitute for the capital gains register in Part D. Only the last four characters of the PAN are shown. Stamp duty of 0.005 per cent applies to purchases; Securities Transaction Tax applies on redemption of equity-oriented units; tax is deducted at source on dividend income above INR 5,000 in a financial year."),
        P(f"<b>3. Regular and Direct Plans; Distributor Commission Disclosure.</b> The holdings below are Regular Plans, through which the AMC pays {me} a trail commission from the scheme's expense ratio (illustrative range 0.45 to 1.10 per cent of average assets per year, varying by scheme). A Direct Plan of the same scheme has no distributor commission and a lower expense ratio. {me} does not charge the investor a separate fee for these transactions, and the investor may move to a Direct Plan at any time by switching or redeeming and reinvesting, which may attract exit load and tax."),
        P(f"<b>4. Service Standards.</b> The table below sets the regulatory position against the commitments {CO.legal} makes to its investors. Customer care: {CO.inv_tf}; email care@{CO.dom}."),
        grid_table(svc, [44 * mm, 66 * mm, FRAME_W - 110 * mm]),
    ]

    p2 = [
        P("Part D. High-Density Data Grids", H2),
        P("Table 1. Portfolio Valuation Grid as on 30 September 2026 (Scheme Name, Folio Number, Available Units, Live NAV and Current Market Value in INR).", SMALL),
        grid_table(t_val, [40 * mm, 19 * mm, 17 * mm, 20 * mm, 19 * mm, 24 * mm, 23 * mm, FRAME_W - 162 * mm], right_cols=(2, 3, 4, 5, 6, 7)),
        Spacer(1, 4),
        P("Table 2. Realised Capital Gains Register, Financial Year 2026-27 (1 April to 30 September 2026). Long-Term Capital Gains (LTCG) are separated from Short-Term Capital Gains (STCG); holding period is counted from purchase to redemption.", SMALL),
        grid_table(t_cg, [34 * mm, 11 * mm, 19 * mm, 17 * mm, 19 * mm, 18 * mm, 14 * mm, 12 * mm, 14 * mm, FRAME_W - 158 * mm], right_cols=(3, 5, 6, 7, 9)),
        Spacer(1, 4),
        P("Table 3. Capital Gains Tax Distribution (indicative, for information; not tax advice).", SMALL),
        grid_table(t_tax, [64 * mm, 25 * mm, 38 * mm, 25 * mm, 12 * mm, FRAME_W - 164 * mm], right_cols=(1, 3, 4, 5)),
        Spacer(1, 4),
        P("Figure 1. Pie chart: portfolio asset allocation distribution weights by category (Equity, Hybrid, Debt, Liquid) computed from the market values in Table 1.", SMALL),
        chart_flowable(pie, FRAME_W * 0.80),
        P("Figure 1 caption: Share of total market value by scheme category as on 30 September 2026.", CAPTION),
    ]

    p3 = [
        P("Part E. Statutory Disclosures, Risk Framework and Audit Sign-Off", H2),
        P("<b>5. Capital Gains Rules Applied in This Statement.</b> For equity-oriented schemes, units held for more than twelve months give long-term capital gains, taxed at 12.5 per cent on the gain above INR 1,25,000 in a financial year; units held for twelve months or less give short-term capital gains, taxed at 20 per cent. Gains on debt and liquid schemes acquired on or after 1 April 2023 are treated here as short-term capital gains taxed at the investor's slab rate. Surcharge and 4 per cent health and education cess are additional. Units are matched on a first-in-first-out (FIFO) basis. Confirm the position with a tax professional."),
        P("<b>6. Mutual Fund Investment Risk Disclosure.</b> Mutual fund investments are subject to market risks; read all scheme-related documents carefully. Past performance does not guarantee future returns. NAV may go up or down depending on factors affecting securities markets. Liquid and debt schemes are subject to interest-rate, credit and liquidity risk. Each scheme's Riskometer is available on the scheme page and is reviewed monthly."),
        P("<b>7. Exit Load, Systematic Plans and Dividends.</b> Exit load, if any, is charged per the Scheme Information Document in force on the date of the original purchase. SIP, SWP and STP instalments appear as separate transaction lines. Dividends under the Income Distribution cum Capital Withdrawal (IDCW) option, where chosen, appear as payout or reinvestment lines and are not part of the growth option holdings above."),
        P(f"<b>8. Folio Integrity, KYC and Nomination.</b> Every folio must carry a valid PAN, a KYC-validated status and a nomination or a signed opt-out declaration; folios without these may be frozen for debits until the requirement is met. Verify unit balances, NAV and masked identity details against your records and report any discrepancy to {me} or the AMC or RTA within 30 days of receipt, failing which the statement is taken as correct."),
        P(f"<b>9. Reconciliation Controls.</b> Before display and dispatch, {me} reconciles the data received from the depositories and RTAs with the portfolio on its platform: unit balances against the RTA records, NAV against the published AMFI NAV file for the date, and the valuation grid total against the sum of individual market values. Any variance above INR 1 triggers manual review. The statement is a password-protected PDF (PAN in capital letters followed by date of birth); investor data is used only to service the account and comply with law."),
        H("Investor Grievance Redressal and Escalation Matrix"),
        escalation_table([
            ["Level 1", f"{me} Investor Service Desk", f"Customer care {CO.inv_tf} (Mon to Sat); care@{CO.dom}", "Acknowledged in 1 working day; resolved within 10 working days"],
            ["Level 2", "Investor Grievance Officer: Priyanka Nair Menon", f"investor.grievance@{CO.dom}; {CO.house}", "Resolved within 21 days of the complaint"],
            ["Level 3", "Compliance Officer: Imran Ahmed Sheikh", f"compliance@{CO.dom}", "Final reply within 30 days"],
            ["Level 4", "SEBI SCORES and the Online Dispute Resolution (ODR) platform for complaints against an AMC or RTA; AMFI for complaints against the distributor", "Lodge online on SEBI SCORES or the ODR platform; write to AMFI under its Code of Conduct", "If not resolved within 21 days or the reply is unsatisfactory"],
        ]),
        Spacer(1, 3),
        H("Audit and Dispatch Sign-Off"),
        signoff_table([
            ("Registrar Operations Head", "Lakshmi Venkataraman", "Vice President, Statement Operations", "02 Oct 2026"),
            ("Distribution Compliance", "Imran Ahmed Sheikh", "Compliance Officer", "02 Oct 2026"),
            ("Investor Grievance Officer", "Priyanka Nair Menon", "Investor Grievance Officer", "03 Oct 2026"),
            ("Internal Auditor Review", "Gopal Krishnan and Partners", "Chartered Accountants, Engagement Partner", "03 Oct 2026"),
        ]),
    ]
    return code, [p1, p2, p3]


# ═════════════════════════ DOCUMENT 3 — INSURANCE (corporate agent) ═════════════════════════
def doc_insurance() -> tuple[str, list[list]]:
    code = f"{CO.ini}-GI-FCP-2026-0518"
    me = CO.short
    ins = CO.partner_insurer
    GST = 0.18
    sched = [  # loc, location, asset class, SI INR, basis, ded_pct, ded_min, risk_idx, rate per mille, our share %
        ("L1", "Pune Manufacturing Plant, Chakan", "Building and civil works", 185_000_000, "Reinstatement value", 5, 50_000, 62, 0.62, 100),
        ("L1", "Pune Manufacturing Plant, Chakan", "Plant and machinery", 420_000_000, "Reinstatement value", 5, 100_000, 66, 0.71, 100),
        ("L2", "Ahmedabad Distribution Warehouse", "Stock in trade", 260_000_000, "Cost or market value, whichever is lower", 5, 100_000, 74, 0.88, 70),
        ("L3", "Nagpur Cold Storage Unit", "Building", 94_000_000, "Reinstatement value", 5, 50_000, 58, 0.55, 100),
        ("L3", "Nagpur Cold Storage Unit", "Stock (temperature controlled)", 146_000_000, "Cost or market value, whichever is lower", 7.5, 150_000, 81, 1.12, 100),
        ("L4", "Coimbatore Regional Office", "Building, furniture and fixtures", 62_000_000, "Reinstatement value", 5, 25_000, 41, 0.38, 100),
    ]
    t_sched = [["Loc.", "Insured Location", "Asset Class", "Sum Insured (INR)", "Valuation Basis", "Deductible", "Risk Index (0-100)", "Our Share (%)", "Liability Cap, this policy (INR)", "Rate (per mille)", "Net Premium (INR)"]]
    tot_si = tot_cap = tot_prem = 0.0
    for loc, lname, ac, si, basis, dp, dmin, idx, rate, share in sched:
        cap = si * share / 100
        prem = cap * rate / 1000
        tot_si += si
        tot_cap += cap
        tot_prem += prem
        t_sched.append([loc, lname, ac, ind(si), basis, f"{dp:g}% of claim, min INR {ind(dmin)}", str(idx), f"{share}", ind(cap), f"{rate:.2f}", ind(prem, 2)])
    gst_amt = tot_prem * GST
    t_sched.append(["Total", "", "", ind(tot_si), "", "", "", "", ind(tot_cap), "-", ind(tot_prem, 2)])
    t_sched.append(["", "GST at 18%", "", "", "", "", "", "", "", "-", ind(gst_amt, 2)])
    t_sched.append(["", "Total premium payable including GST", "", "", "", "", "", "", "", "-", ind(tot_prem + gst_amt, 2)])

    loss = sorted(zip(["Cold-chain stock", "Stock in trade", "Plant and machinery", "Electronic equipment", "Buildings", "Furniture and fixtures"],
                      [0.91, 0.68, 0.42, 0.55, 0.27, 0.18]), key=lambda x: -x[1])
    bar = hbar_chart("Historic Fire and Allied Perils Loss Factor by Asset Class", [a for a, _ in loss], [b for _, b in loss],
                     "Burning cost: incurred claims as per cent of sum insured, 5-year average",
                     "Sample underwriting data for illustration; replace with the insurer's actual portfolio experience.")

    perils = [
        ["Peril", "Covered", "Basis / Limit"],
        ["Fire, lightning, explosion and implosion", "Yes", "Up to the sum insured per location"],
        ["Aircraft damage, impact damage by vehicles or animals", "Yes", "Up to the sum insured per location"],
        ["Riot, strike, malicious damage", "Yes", "Up to the sum insured per location"],
        ["Storm, cyclone, typhoon, tempest, flood and inundation (STFI)", "Yes", "Deductible 5% of claim, minimum INR 50,000"],
        ["Subsidence, landslide including rockslide", "Yes", "Excludes settlement of foundations unless caused by an insured peril"],
        ["Bursting or overflowing of water tanks, apparatus and pipes; sprinkler leakage", "Yes", "Up to the sum insured per location"],
        ["Bush fire and missile testing operations", "Yes", "Up to the sum insured per location"],
        ["Earthquake (fire and shock)", "Optional add-on", "Not opted in this policy period"],
        ["Terrorism", "Optional add-on", "Not opted in this policy period"],
    ]
    commit = [
        ["Claim Stage", f"Regulatory and Insurer Standard ({ins})", f"{me} Servicing Commitment"],
        ["Claim intimation", "Policyholder notifies the Insurer immediately", f"{me} registers the claim with the Insurer within 2 hours of your call or platform request, 24x7"],
        ["Surveyor appointment", "Insurer appoints a licensed surveyor within 24 hours of notice where a survey is required", f"{me} follows up with the Insurer at 12 hours and shares the surveyor's contact with you"],
        ["Survey report", "Within 30 days of appointment, extendable by 15 days with reasons (Section 64UM)", "Status update to you every 7 days"],
        ["Settlement", "Insurer settles within 30 days of the survey report or last necessary document", f"{me} escalates to the Insurer's Grievance Redressal Officer if payment is not made by day 20"],
        ["Interest on delay", "2 per cent above the bank rate, payable by the Insurer", "Reminds the Insurer to include it with the payment; no charge to you"],
    ]

    p1 = [
        P(f"{me.upper()}: FLEXI COMMERCIAL PROPERTY POLICY (STANDARD FIRE AND ALLIED PERILS)", TITLE),
        P(f"Policy Wording, Schedule and Servicing Guide No. {code} &nbsp;|&nbsp; Period of Insurance 01 October 2026 to 30 September 2027 &nbsp;|&nbsp; Issued at Pune", SUB),
        H("Part A. Enterprise Metadata and Data Classification"),
        kv_table([
            ("Industry Pillar", "Insurance (distribution and policy servicing)"),
            ("Target Enterprise Document Type", "Flexi Commercial Property Policy (Standard Fire and Allied Perils / Special Perils wording) with sample policy schedule and servicing guide"),
            ("Regulatory Framework", "Insurance Act, 1938 (including Section 64UM on surveyors and loss assessors and Section 64VB on premium payment); IRDAI Act, 1999; IRDAI regulations on protection of policyholders' interests, on corporate agents and on claim settlement and grievance redressal timelines; Insurance Ombudsman Rules, 2017"),
            ("Document Identification Code", code),
            ("Classification Level", "Strictly Confidential"),
            ("Distribution and Servicing Partner", f"{identity_line()}; IRDAI-registered Corporate Agent, registration {CO.ca_reg} (illustrative). Helpline {CO.ins_tf} (24x7); email claims@{CO.dom}"),
            ("Insurer (risk carrier)", f"{ins} (IRDAI Registration No. 147). The Insurer underwrites the risk and settles claims"),
            ("Sample Policyholder", "Narmada Agro Foods Private Limited, Survey No. 118, Chakan Industrial Belt, Pune, Maharashtra 410501 (illustrative customer)"),
        ]),
        Spacer(1, 3),
        H("Part B. Statutory Risk Management Structure for Commercial Assets"),
        P(f"<b>1. Insuring Clause.</b> In consideration of the premium paid, {ins} (the Insurer) agrees to indemnify the Policyholder against loss of or damage to the property described in the Schedule, occurring during the Period of Insurance, by any peril listed in the Perils Grid, up to the Sum Insured for each item at each location and subject to the deductibles, conditions and exclusions of this policy."),
        P("<b>2. Reinstatement Value Definition.</b> Reinstatement Value means the cost of replacing or restoring the insured property with new property of the same kind and capacity, without deduction for depreciation, including freight, customs duty, erection charges, architect and engineer fees and any taxes that are not recoverable. Where the Sum Insured is on a reinstatement value basis, the Insurer pays the cost of actual reinstatement but only once the property has been reinstated or replaced; until then it pays on the basis of the market value of the property immediately before the loss."),
        P("<b>3. Standard Fire and Allied Perils Coverage Grid.</b> Only perils marked Yes are insured; optional extensions are insured only when specifically opted for and endorsed on the Schedule."),
        grid_table(perils, [86 * mm, 28 * mm, FRAME_W - 114 * mm]),
        Spacer(1, 2),
        P("<b>4. Basis of Sum Insured and Under-Insurance.</b> If at the time of loss the Sum Insured on any item is less than the value of that item at the same location (the Condition of Average), the Insurer pays only the proportion that the Sum Insured bears to the value at risk. The Policyholder shall review and declare values at least once in each Period of Insurance."),
        P(f"<b>5. Role of {me} as Corporate Agent.</b> {CO.legal} solicits and services this policy as an IRDAI-registered Corporate Agent. It does not underwrite the risk, decide a claim or hold premium: premium is paid directly to the Insurer through the Insurer's payment channel, and no cover subsists until the Insurer receives it (Section 64VB). {me} is paid a commission by the Insurer within IRDAI limits, which is included in the premium and not charged separately. Cover runs for twelve months and the Insurer sends a renewal notice at least thirty days before expiry."),
    ]

    p2 = [
        P("Part C. High-Density Policy Schedule Matrix", H2),
        P("Table 1. Policy Schedule with deductibles, risk exposure indices, other-insurance apportionment (deduplication of overlapping cover at the same location so that no loss is paid twice) and liability caps across the commercial asset locations. Liability Cap equals Sum Insured multiplied by Our Share. Net Premium equals Liability Cap multiplied by the rate per mille divided by one thousand. All amounts are in Indian Rupees (INR).", SMALL),
        grid_table(t_sched, [8 * mm, 22 * mm, 19 * mm, 20 * mm, 19 * mm, 19 * mm, 12 * mm, 10 * mm, 20 * mm, 9 * mm, FRAME_W - 158 * mm], right_cols=(3, 6, 7, 8, 9, 10)),
        Spacer(1, 3),
        P("Notes to Table 1. Our Share below 100 per cent at Location L2 reflects an other-insurance arrangement under which a second insurer covers 30 per cent of the stock at the Ahmedabad Distribution Warehouse; in the event of loss the two insurers contribute rateably and the Policyholder cannot recover more than the actual loss in total. The Risk Index is the Insurer's internal underwriting index from 0 (lowest) to 100 (highest), combining construction class, occupancy hazard, fire protection, flood and seismic zone.", SMALL),
        Spacer(1, 4),
        P("Figure 1. Horizontal bar chart: historic loss factors (burning cost, per cent of sum insured, five-year average) across asset classes. The cold-chain stock class shows the highest loss factor (0.91 per cent) and furniture and fixtures the lowest (0.18 per cent). The chart supports the rate per mille chosen for each asset class in Table 1.", SMALL),
        chart_flowable(bar, FRAME_W * 0.92),
        P("Figure 1 caption: Historic insurance risk loss factors by asset class (sample data).", CAPTION),
    ]

    p3 = [
        P("Part D. Claims Conditions, Service Commitments, Grievance Redressal and Sign-Off", H2),
        P("<b>6. Notice of Loss and Claims Procedure.</b> On any loss or damage the Policyholder shall (a) notify the Insurer immediately, in writing, by email or through the claims helpline, and the police in case of theft, riot or malicious damage; (b) take all reasonable steps to minimise loss; (c) preserve the damaged property for survey; and (d) submit a claim form with the documents required, including a fire brigade report where applicable, stock and fixed asset registers, invoices and a statement of loss."),
        P(f"<b>7. Turnaround Time (TAT) for Claims.</b> The Insurer decides and pays the claim; {me} supports the Policyholder at each stage. The table sets the regulatory and Insurer standard against {me}'s servicing commitments. Where a claim requires investigation, the investigation is completed within 30 days and settlement made within 45 days from receipt of the last document."),
        grid_table(commit, [30 * mm, 76 * mm, FRAME_W - 106 * mm]),
        Spacer(1, 2),
        P("<b>8. Exclusions.</b> No cover for war, invasion or nuclear risks; wilful act or gross negligence of the Policyholder; pollution or contamination unless caused by an insured peril; property in transit; consequential loss or loss of profit (available only under a separate business interruption cover); and loss of stock caused by change in temperature unless the cause is an insured peril."),
        P("<b>9. Subrogation, Contribution, Fraud and Cancellation.</b> On payment of a claim the Insurer is subrogated to the Policyholder's rights against any third party. Where the same property is covered by other insurance, the Insurer is liable only for its rateable proportion, as reflected in the Our Share column. A fraudulent claim or false declaration forfeits the claim and the policy is void at the Insurer's option under the Insurance Act, 1938. The Policyholder may cancel on fifteen days' written notice with a pro-rata or short-period refund if no claim has been made; the Insurer may cancel on seven days' notice for non-disclosure, fraud or non-payment."),
        H("IRDAI Grievance Redressal Provisions and Escalation Matrix"),
        escalation_table([
            ["Level 1", f"{me} Insurance Service Desk", f"{CO.ins_tf} (24x7); care@{CO.dom}", "Acknowledged within 3 days; resolved within 14 days (IRDAI standard)"],
            ["Level 2", f"{me} Grievance Officer: Neha Prakash Walia", f"grievance@{CO.dom}; {CO.house}", "Resolved within 10 working days of escalation"],
            ["Level 3", f"Grievance Redressal Officer of the Insurer ({ins})", "Contact details in the policy schedule and on the Insurer's website", "Acknowledged within 3 days; resolved within 14 days"],
            ["Level 4", "IRDAI Integrated Grievance Management System (Bima Bharosa)", "Register online through the IRDAI portal; the Insurer must respond within the regulatory timelines", "If unresolved after Level 3 or unsatisfactory"],
            ["Level 5", "Insurance Ombudsman of the territorial jurisdiction; Consumer Commission", "Under the Insurance Ombudsman Rules, 2017, if the complaint was rejected or unanswered for 30 days; or the Consumer Disputes Redressal Commission under the Consumer Protection Act, 2019", "As per the Rules and the Act"],
        ]),
        Spacer(1, 3),
        H("Underwriting, Distribution and Compliance Sign-Off"),
        signoff_table([
            ("Insurer Underwriter", "Deepak Raghunathan Bhat", f"Senior Manager, Property Underwriting, {ins}", "28 Sep 2026"),
            ("Risk Surveyor Acknowledgement", "Suresh Chandrasekaran", "IRDAI Licensed Surveyor and Loss Assessor", "27 Sep 2026"),
            ("Corporate Agent Compliance Officer", "Neha Prakash Walia", f"Chief Compliance Officer, {me}", "29 Sep 2026"),
            ("Policyholder Authorised Signatory", "Harish Menon Verghese", "Director, Narmada Agro Foods Private Limited", "30 Sep 2026"),
        ]),
    ]
    return code, [p1, p2, p3]


# ───────────────────────── main ─────────────────────────
JOBS = [
    ("banking_rtl_standard_facility_agreement.pdf", doc_banking, lambda: f"Banking | {CO.short} | Rupee Term Loan Standard Facility Agreement"),
    ("finserv_mutual_fund_ecas_and_servicing.pdf", doc_finserv, lambda: f"Financial Services | {CO.short} | eCAS and Investor Servicing Guide"),
    ("insurance_flexi_commercial_property.pdf", doc_insurance, lambda: f"Insurance | {CO.short} | Flexi Commercial Property Policy and Servicing Guide"),
]


def main() -> None:
    global CO
    ap = argparse.ArgumentParser(description="Generate the company's three BFSI enterprise documents (3 pages each).")
    ap.add_argument("--legal-name", default="AuraVance Digital FinTech Private Limited")
    ap.add_argument("--short", default=None, help="short brand name (default: first word of the legal name)")
    ap.add_argument("--cin", default="U65110MH2026PTC384152")
    ap.add_argument("--pan", default="AAACF2026N")
    ap.add_argument("--domain", default="auravance.aura", help="email / web domain used in contact details")
    ap.add_argument("--out", default=str(OUT_DIR), help="output directory")
    args = ap.parse_args()
    CO = Co(args.legal_name, args.cin, args.pan, args.domain, args.short)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for filename, builder, banner in JOBS:
        code, pages = builder()
        build_pdf(out / filename, code, banner(), "Strictly Confidential", pages)
        print(f"wrote {out / filename}")


if __name__ == "__main__":
    main()
