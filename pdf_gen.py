from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.lib.utils import ImageReader
from datetime import datetime
import io
import re
import base64


# =========================
# Helpers
# =========================
def _safe(text):
    return (text or "").strip()


def _brl_from_cents(cents: int) -> str:
    try:
        cents = int(cents or 0)
    except Exception:
        cents = 0
    v = cents / 100.0
    return f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _parse_price_to_cents(price_str: str) -> int:
    s = _safe(price_str)
    if not s:
        return 0
    s = s.lower().replace("r$", "").strip()
    s = re.sub(r"[^\d,.\-]", "", s)
    if not s:
        return 0
    has_comma = "," in s
    has_dot = "." in s
    if has_comma and has_dot:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif has_comma:
        s = s.replace(",", ".")
    try:
        return int(round(float(s) * 100))
    except Exception:
        return 0


def _fmt_qty(q):
    try:
        f = float(q)
        if f.is_integer():
            return str(int(f))
        return str(f).rstrip("0").rstrip(".")
    except Exception:
        return str(q or "")


def _wrap_lines(text, max_w, font="Helvetica", size=9):
    text = text or ""
    lines = []
    for paragraph in text.splitlines():
        p = paragraph.strip()
        if not p:
            lines.append("")
            continue
        words = p.split()
        line = ""
        for w in words:
            test = (line + " " + w).strip()
            if stringWidth(test, font, size) <= max_w:
                line = test
            else:
                if line:
                    lines.append(line)
                line = w
        if line:
            lines.append(line)
    return lines or [""]


# =========================
# Color palette
# =========================
DARK       = colors.HexColor("#1a2744")
HEADER_BG  = colors.HexColor("#f2f4f8")
ROW_BG     = colors.HexColor("#f9fafc")
BORDER     = colors.HexColor("#d8dde8")
TEXT_MAIN  = colors.HexColor("#1a2744")
TEXT_GRAY  = colors.HexColor("#5a6378")
RED_DISC   = colors.HexColor("#c0392b")
COL_BORDER = colors.HexColor("#c8cdd8")
WHITE      = colors.white


# =========================
# Low-level draw helpers
# =========================
def _rect(cv, x, y_top, w, h, fill=WHITE, stroke_color=BORDER, stroke=1, radius=0):
    cv.setFillColor(fill)
    cv.setStrokeColor(stroke_color)
    cv.setLineWidth(0.5)
    if radius:
        cv.roundRect(x, y_top - h, w, h, radius, fill=1, stroke=stroke)
    else:
        cv.rect(x, y_top - h, w, h, fill=1, stroke=stroke)


def _text(cv, txt, x, y, font="Helvetica", size=9, color=TEXT_MAIN, align="left"):
    cv.setFont(font, size)
    cv.setFillColor(color)
    s = str(txt)
    if align == "right":
        cv.drawRightString(x, y, s)
    elif align == "center":
        cv.drawCentredString(x, y, s)
    else:
        cv.drawString(x, y, s)


def _hline(cv, x1, x2, y, color=BORDER, width=0.5):
    cv.setStrokeColor(color)
    cv.setLineWidth(width)
    cv.line(x1, y, x2, y)


def _vline(cv, x, y1, y2, color=BORDER, width=0.5):
    cv.setStrokeColor(color)
    cv.setLineWidth(width)
    cv.line(x, y1, x, y2)


# =========================
# Layout constants
# =========================
ML = 2.0 * cm
MR = 2.0 * cm


def _cw(width):
    return width - ML - MR


# =========================
# Header
# =========================
def _draw_header(cv, width, height, data, logo_img, public_id):
    company_name = _safe(data.get("company_name"))
    author_name  = _safe(data.get("author_name"))
    phone        = _safe(data.get("phone"))
    email        = _safe(data.get("author_email"))
    website      = _safe(data.get("website"))
    display_name = company_name or author_name or "Orçamento"

    logo_size = 1.4 * cm
    logo_x    = ML
    logo_y    = height - 1.0 * cm - logo_size

    if logo_img is not None:
        try:
            cv.drawImage(logo_img, logo_x, logo_y, logo_size, logo_size,
                         mask="auto", preserveAspectRatio=True)
        except Exception:
            _rect(cv, logo_x, logo_y + logo_size, logo_size, logo_size, fill=DARK, stroke=0)
    else:
        _rect(cv, logo_x, logo_y + logo_size, logo_size, logo_size, fill=DARK, stroke=0)
        _text(cv, display_name[:2].upper(),
              logo_x + logo_size / 2, logo_y + logo_size / 2 - 4,
              "Helvetica-Bold", 14, WHITE, "center")

    name_x = logo_x + logo_size + 0.4 * cm
    name_y = height - 1.1 * cm
    _text(cv, display_name, name_x, name_y, "Helvetica-Bold", 14, TEXT_MAIN)

    sub_parts = []
    if phone:   sub_parts.append(f"Tel: {phone}")
    if email:   sub_parts.append(f"Email: {email}")
    if website: sub_parts.append(website)
    for i, part in enumerate(sub_parts[:3]):
        _text(cv, part, name_x, name_y - (i + 1) * 0.38 * cm, "Helvetica", 8, TEXT_GRAY)

    rule_y = height - 2.6 * cm
    _hline(cv, ML, width - MR, rule_y, BORDER, 0.8)

    title_y = rule_y - 0.65 * cm
    _text(cv, f"ORÇAMENTO Nº {public_id}", width / 2, title_y,
          "Helvetica-Bold", 15, TEXT_MAIN, "center")

    return title_y - 0.5 * cm


# =========================
# Section heading
# =========================
def _section_heading(cv, title, x, y, w):
    h = 0.65 * cm
    _rect(cv, x, y, w, h, fill=HEADER_BG, stroke_color=BORDER, stroke=1)
    _text(cv, title, x + 0.4 * cm, y - h + 0.18 * cm, "Helvetica-Bold", 9.5, TEXT_MAIN)
    return y - h


# =========================
# Page break helper
# =========================
def _ensure_space(cv, y, needed, width, height, draw_hdr_fn):
    if y - needed < 2.5 * cm:
        cv.showPage()
        return draw_hdr_fn() - 0.3 * cm
    return y


# =========================
# Client section
# =========================
def _draw_client_section(cv, data, x, y, w, width, height, draw_hdr_fn):
    client   = _safe(data.get("client_name")) or "-"
    cpf_cnpj = _safe(data.get("client_cpf_cnpj"))
    phone    = _safe(data.get("client_phone"))

    rows = [("Cliente:", client)]
    if cpf_cnpj: rows.append(("CPF/CNPJ:", cpf_cnpj))
    if phone:    rows.append(("Telefone:", phone))

    row_h   = 0.55 * cm
    total_h = row_h * len(rows)

    y = _ensure_space(cv, y, 1.2 * cm + total_h, width, height, draw_hdr_fn)
    y = _section_heading(cv, "Dados do Cliente", x, y, w)
    _rect(cv, x, y, w, total_h, fill=WHITE, stroke_color=BORDER, stroke=1)

    yy = y - 0.18 * cm
    for label, val in rows:
        _text(cv, label, x + 0.4 * cm, yy - 0.25 * cm, "Helvetica-Bold", 9, TEXT_MAIN)
        _text(cv, val,   x + 3.5 * cm, yy - 0.25 * cm, "Helvetica", 9, TEXT_MAIN)
        yy -= row_h

    return y - total_h - 0.5 * cm


# =========================
# Details section
# =========================
def _draw_details_section(cv, data, x, y, w, width, height, draw_hdr_fn):
    gen_dt   = datetime.now().strftime("%d/%m/%Y")
    deadline = _safe(data.get("deadline")) or "-"
    status   = _safe(data.get("status")) or "Pendente"

    rows = [("Data Criação:", gen_dt), ("Validade:", deadline), ("Status:", status)]
    row_h   = 0.55 * cm
    total_h = row_h * len(rows)

    y = _ensure_space(cv, y, 1.2 * cm + total_h, width, height, draw_hdr_fn)
    y = _section_heading(cv, "Detalhes do Orçamento", x, y, w)
    _rect(cv, x, y, w, total_h, fill=WHITE, stroke_color=BORDER, stroke=1)

    yy = y - 0.18 * cm
    for label, val in rows:
        _text(cv, label, x + 0.4 * cm, yy - 0.25 * cm, "Helvetica-Bold", 9, TEXT_MAIN)
        _text(cv, val,   x + 3.5 * cm, yy - 0.25 * cm, "Helvetica", 9, TEXT_MAIN)
        yy -= row_h

    return y - total_h - 0.5 * cm


# =========================
# Items table
# =========================
def _draw_items_section(cv, items, x, y, w, width, height, draw_hdr_fn):
    if not items:
        return y

    col_desc  = w * 0.44
    col_qty   = w * 0.10
    col_price = w * 0.23
    # col_sub uses remaining width
    header_h  = 0.65 * cm
    row_h_base = 0.72 * cm

    xs = [
        x,
        x + col_desc,
        x + col_desc + col_qty,
        x + col_desc + col_qty + col_price,
        x + w,
    ]

    # Normalise items
    norm = []
    for it in items:
        desc     = _safe(it.get("description") or it.get("desc"))
        qty      = _fmt_qty(it.get("qty") or 1)
        sub_desc = _safe(it.get("sub_description") or it.get("sub_desc"))

        unit_price_cents = it.get("unit_price_cents")
        if unit_price_cents is None:
            unit_price_cents = _parse_price_to_cents(str(it.get("unit_price") or ""))
        unit_price_cents = int(unit_price_cents or 0)

        discount_pct = 0.0
        try:
            discount_pct = float(it.get("discount_pct") or it.get("discount_percent") or 0)
        except Exception:
            pass

        discount_cents = int(round(unit_price_cents * discount_pct / 100))
        net_unit_cents = unit_price_cents - discount_cents

        line_total_cents = int(it.get("line_total_cents") or 0)
        if line_total_cents <= 0:
            try:
                line_total_cents = int(round(float(qty) * net_unit_cents))
            except Exception:
                line_total_cents = 0

        has_extra = bool(sub_desc or discount_pct)
        norm.append({
            "desc":       desc,
            "sub_desc":   sub_desc,
            "qty":        qty,
            "unit_price": _brl_from_cents(unit_price_cents),
            "discount":   f"{discount_pct:.0f}% (-{_brl_from_cents(discount_cents)})" if discount_pct else "",
            "line_total": _brl_from_cents(line_total_cents),
            "has_extra":  has_extra,
        })

    y = _ensure_space(cv, y, 1.2 * cm + header_h + row_h_base * 2, width, height, draw_hdr_fn)
    y = _section_heading(cv, "Itens do Orçamento", x, y, w)

    # Header row
    _rect(cv, x, y, w, header_h, fill=HEADER_BG, stroke_color=BORDER, stroke=1)
    hdr_y = y - header_h + 0.18 * cm
    _text(cv, "Descrição do Item/Serviço",  xs[0] + 0.4 * cm,          hdr_y, "Helvetica-Bold", 8.5, TEXT_MAIN)
    _text(cv, "Qtde.",                       xs[1] + col_qty / 2,       hdr_y, "Helvetica-Bold", 8.5, TEXT_MAIN, "center")
    _text(cv, "Preço Unit. / Desc.",         xs[2] + (xs[3]-xs[2]) / 2, hdr_y, "Helvetica-Bold", 8.5, TEXT_MAIN, "center")
    _text(cv, "Subtotal",                    xs[4] - 0.4 * cm,          hdr_y, "Helvetica-Bold", 8.5, TEXT_MAIN, "right")
    for xi in xs[1:-1]:
        _vline(cv, xi, y, y - header_h, COL_BORDER)
    y -= header_h

    # Data rows
    for i, row in enumerate(norm):
        rh = row_h_base * 1.6 if row["has_extra"] else row_h_base
        y = _ensure_space(cv, y, rh + 0.2 * cm, width, height, draw_hdr_fn)

        fill = ROW_BG if i % 2 == 0 else WHITE
        _rect(cv, x, y, w, rh, fill=fill, stroke_color=BORDER, stroke=1)

        # Vertical center: baseline offset = rh/2 - font_size/2 (approx 3.2pt for size 9)
        font_offset = 0.11 * cm  # ~3pt, half of 9pt font
        if row["has_extra"]:
            # two-line rows: center the pair, main text above mid, sub below
            pair_h = 0.38 * cm  # gap between main and sub line
            ry = y - rh / 2 + pair_h / 2 + font_offset
        else:
            ry = y - rh / 2 + font_offset

        _text(cv, row["desc"][:65],     xs[0] + 0.4 * cm,          ry, "Helvetica-Bold", 9, TEXT_MAIN)
        if row["sub_desc"]:
            _text(cv, row["sub_desc"][:75], xs[0] + 0.4 * cm, ry - 0.38 * cm, "Helvetica", 7.5, TEXT_GRAY)

        _text(cv, row["qty"],            xs[1] + col_qty / 2,       ry, "Helvetica", 9, TEXT_MAIN, "center")

        pc = xs[2] + (xs[3] - xs[2]) / 2
        _text(cv, row["unit_price"],     pc, ry, "Helvetica", 9, TEXT_MAIN, "center")
        if row["discount"]:
            _text(cv, row["discount"],   pc, ry - 0.38 * cm, "Helvetica", 7.5, RED_DISC, "center")

        _text(cv, row["line_total"],     xs[4] - 0.4 * cm, ry, "Helvetica-Bold", 9, TEXT_MAIN, "right")

        for xi in xs[1:-1]:
            _vline(cv, xi, y, y - rh, COL_BORDER)

        y -= rh

    return y - 0.2 * cm


# =========================
# Total row
# =========================
def _draw_total(cv, total_cents, price_str, x, y, w, width, height, draw_hdr_fn):
    total_brl = _brl_from_cents(total_cents) if total_cents > 0 else (_safe(price_str) or "-")
    box_h = 0.65 * cm
    y = _ensure_space(cv, y, box_h + 0.2 * cm, width, height, draw_hdr_fn)
    _rect(cv, x, y, w, box_h, fill=HEADER_BG, stroke_color=BORDER, stroke=1)
    _text(cv, f"Total Geral: {total_brl}", x + w - 0.4 * cm,
          y - box_h + 0.20 * cm, "Helvetica-Bold", 11, TEXT_MAIN, "right")
    return y - box_h - 0.5 * cm


# =========================
# Observations
# =========================
def _draw_observations(cv, lines, x, y, w, width, height, draw_hdr_fn):
    if not lines:
        return y
    line_h = 0.50 * cm
    pad_x  = 0.4 * cm
    pad_y  = 0.3 * cm
    box_h  = pad_y * 2 + line_h * len(lines)

    y = _ensure_space(cv, y, 1.2 * cm + box_h, width, height, draw_hdr_fn)
    y = _section_heading(cv, "Observações", x, y, w)
    _rect(cv, x, y, w, box_h, fill=WHITE, stroke_color=BORDER, stroke=1)

    yy = y - pad_y - 0.22 * cm
    for ln in lines:
        _text(cv, ln, x + pad_x, yy, "Helvetica", 9, TEXT_MAIN)
        yy -= line_h
    return y - box_h - 0.5 * cm


# =========================
# Client signature (single line)
# =========================
def _draw_signature(cv, client_name, x, y, w):
    sig_y  = y - 0.8 * cm
    sig_w  = 7.0 * cm
    sig_cx = x + w / 2  # centered on page

    _hline(cv, sig_cx - sig_w / 2, sig_cx + sig_w / 2, sig_y, TEXT_GRAY, 0.6)
    _text(cv, client_name or "Assinatura do Cliente",
          sig_cx, sig_y - 0.40 * cm, "Helvetica", 8.5, TEXT_GRAY, "center")


# =========================
# Accept URL footer
# =========================
def _draw_accept_url(cv, accept_url, x, y, footer_y=1.8 * cm):
    if not accept_url:
        return
    _text(cv, "Aceite online:", x, footer_y + 0.45 * cm, "Helvetica-Bold", 9, TEXT_MAIN)
    _text(cv, accept_url[:110],  x, footer_y + 0.05 * cm, "Helvetica", 8, TEXT_GRAY)


# =========================
# Watermark (free tier)
# =========================
def _draw_watermark(cv, width):
    cv.setFont("Helvetica", 8)
    cv.setFillColor(TEXT_GRAY)
    cv.drawRightString(width - MR, 1.0 * cm, "Gerado com PropoFlow • Remova no PRO")


# =========================
# Main entry point
# =========================
def generate_proposal_pdf(data: dict) -> bytes:
    buffer = io.BytesIO()
    cv = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    is_pro    = bool(data.get("is_pro", False))
    public_id = str(data.get("public_id", "0001"))

    logo_img = None
    if data.get("logo_b64"):
        try:
            logo_bytes = base64.b64decode(data["logo_b64"])
            logo_img = ImageReader(io.BytesIO(logo_bytes))
        except Exception:
            logo_img = None

    x = ML
    w = _cw(width)

    def draw_header():
        return _draw_header(cv, width, height, data, logo_img, public_id)

    y = draw_header()
    y -= 0.5 * cm

    # ── Dados do Cliente ──────────────────────────────────────
    y = _draw_client_section(cv, data, x, y, w, width, height, draw_header)

    # ── Detalhes do Orçamento ─────────────────────────────────
    y = _draw_details_section(cv, data, x, y, w, width, height, draw_header)

    # ── Itens do Orçamento ────────────────────────────────────
    items = data.get("items") or []
    if items:
        y = _draw_items_section(cv, items, x, y, w, width, height, draw_header)

    # ── Total Geral ───────────────────────────────────────────
    try:
        total_cents = int(data.get("total_cents") or 0)
    except Exception:
        total_cents = _parse_price_to_cents(data.get("price") or "")

    if total_cents > 0 or data.get("price"):
        y = _draw_total(cv, total_cents, data.get("price"), x, y, w, width, height, draw_header)

    # ── Observações (etapas de pagamento + termos) ─────────────
    obs_lines = []
    for st in (data.get("payment_stages") or []):
        title = _safe(st.get("title"))
        amt   = _brl_from_cents(int(st.get("amount_cents") or 0))
        pct   = st.get("percent")
        obs_lines.append(f"{title}: {amt} ({pct}%)" if pct is not None else f"{title}: {amt}")
    for t in (data.get("payment_terms") or []):
        t = _safe(t)
        if t:
            obs_lines.append(t)

    if obs_lines:
        y = _draw_observations(cv, obs_lines, x, y, w, width, height, draw_header)

    # ── Assinatura do Cliente (única linha, centralizada) ──────
    client_name = _safe(data.get("client_name"))
    _draw_signature(cv, client_name, x, y, w)

    # ── Aceite online ─────────────────────────────────────────
    accept_url = _safe(data.get("accept_url"))
    if accept_url:
        _draw_accept_url(cv, accept_url, x, y)

    # ── Marca d'água (plano gratuito) ─────────────────────────
    if not is_pro:
        _draw_watermark(cv, width)

    cv.save()
    buffer.seek(0)
    return buffer.getvalue()