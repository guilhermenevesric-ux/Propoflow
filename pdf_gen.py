# pdf_gen.py
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
    """
    Aceita:
    - R$ 1.234,56
    - 1234,56
    - 1234.56
    - 1234
    """
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
        # usa o último separador como decimal
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


def _wrap_lines(text, max_w, font="Helvetica", size=10):
    text = text or ""
    paragraphs = text.splitlines() if text else [""]
    lines = []

    for paragraph in paragraphs:
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

    if not lines:
        lines = [""]

    return lines


def _draw_text_lines(c, lines, x, y, font="Helvetica", size=10, leading=12.5, color=(0.12, 0.16, 0.26)):
    c.setFont(font, size)
    c.setFillColorRGB(*color)
    for ln in lines:
        c.drawString(x, y, ln)
        y -= leading
    return y


def _ensure_space(c, y, needed, width, height, draw_header_fn):
    if y - needed < 2.2 * cm:
        c.showPage()
        draw_header_fn()
        return height - 3.4 * cm
    return y


def _draw_box(c, x, y_top, w, h, fill_rgb=(1, 1, 1), stroke_rgb=(0.86, 0.89, 0.96), radius=0.25 * cm, stroke=1):
    c.setFillColorRGB(*fill_rgb)
    c.setStrokeColorRGB(*stroke_rgb)
    c.roundRect(x, y_top - h, w, h, radius, fill=1, stroke=stroke)


def _normalize_items(items):
    out = []
    for it in items or []:
        desc = _safe(it.get("description") or it.get("desc"))
        qty = _fmt_qty(it.get("qty") or 1)

        unit_price_cents = it.get("unit_price_cents")
        if unit_price_cents is None:
            unit_price_cents = _parse_price_to_cents(str(it.get("unit_price") or ""))

        line_total_cents = int(it.get("line_total_cents") or 0)
        if line_total_cents <= 0:
            try:
                line_total_cents = int(round(float(qty) * int(unit_price_cents or 0)))
            except Exception:
                line_total_cents = 0

        out.append({
            "left": f"{desc} ({qty}x)",
            "right": _brl_from_cents(line_total_cents) if line_total_cents > 0 else "—"
        })
    return out


def _normalize_stages(stages):
    out = []
    for st in stages or []:
        title = _safe(st.get("title"))
        amt = _brl_from_cents(int(st.get("amount_cents") or 0))
        pct = st.get("percent")
        if pct is not None:
            out.append(f"{title}: {amt} ({pct}%)")
        else:
            out.append(f"{title}: {amt}")
    return out


def _normalize_terms(terms):
    out = []
    for t in terms or []:
        t = _safe(t)
        if t:
            out.append(t)
    return out


# =========================
# Header
# =========================
def _draw_header(c, width, height, is_pro: bool, brand_title: str, subtitle: str, public_id: str, logo_img=None):
    c.setFillColorRGB(0.06, 0.10, 0.18)
    c.roundRect(2 * cm, height - 2.55 * cm, width - 4 * cm, 1.75 * cm, 0.28 * cm, fill=1, stroke=0)

    if is_pro and logo_img is not None:
        try:
            c.drawImage(logo_img, 2.35 * cm, height - 2.25 * cm, 1.05 * cm, 1.05 * cm, mask="auto")
        except Exception:
            c.setFillColorRGB(0.31, 0.49, 1.00)
            c.roundRect(2.35 * cm, height - 2.15 * cm, 0.95 * cm, 0.95 * cm, 0.24 * cm, fill=1, stroke=0)
    else:
        c.setFillColorRGB(0.31, 0.49, 1.00)
        c.roundRect(2.35 * cm, height - 2.15 * cm, 0.95 * cm, 0.95 * cm, 0.24 * cm, fill=1, stroke=0)

    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 11.5)
    c.drawString(3.55 * cm, height - 1.88 * cm, brand_title)

    c.setFont("Helvetica", 8.5)
    c.setFillColorRGB(0.86, 0.90, 1.00)
    if subtitle:
        c.drawString(3.55 * cm, height - 2.20 * cm, subtitle)
        c.setFillColorRGB(0.31, 0.49, 1.00)
        c.roundRect(width - 7 * cm, height - 2.2 * cm, 4.5 * cm, 0.8 * cm, 0.2 * cm, fill=1, stroke=0)

        c.setFillColorRGB(1, 1, 1)
        c.setFont("Helvetica-Bold", 9)
        c.drawCentredString(width - 4.75 * cm, height - 1.85 * cm, f"{public_id}")

    c.setFont("Helvetica-Bold", 13)
    c.drawRightString(
        width - 2.35 * cm,
        height - 1.90 * cm,
        f"ORÇAMENTO Nº {public_id}"
    )


# =========================
# Seções
# =========================
def _draw_paragraph_section(c, title, lines, x, y, w, width, height, draw_header_fn):
    title_gap = 0.45 * cm
    pad_x = 0.6 * cm
    pad_y = 0.45 * cm
    line_h = 12.5

    idx = 0
    first = True

    while idx < len(lines):
        title_text = title if first else f"{title} (continuação)"
        y = _ensure_space(c, y, needed=2.4 * cm, width=width, height=height, draw_header_fn=draw_header_fn)

        c.setFont("Helvetica-Bold", 10)
        c.setFillColorRGB(0.06, 0.10, 0.18)
        c.drawString(x, y, title_text)
        y -= title_gap

        available_h = y - 2.2 * cm
        max_lines = max(1, int((available_h - (pad_y * 2)) / line_h))
        chunk = lines[idx: idx + max_lines]
        idx += len(chunk)

        box_h = (pad_y * 2) + (len(chunk) * line_h) + 0.15 * cm
        _draw_box(c, x, y, w, box_h, fill_rgb=(1, 1, 1), stroke_rgb=(0.86, 0.89, 0.96), stroke=1)

        text_y = y - pad_y - 0.15 * cm
        _draw_text_lines(c, chunk, x + pad_x, text_y, size=10, leading=line_h)

        y -= box_h + 0.5 * cm
        first = False

    return y


def _draw_list_section(c, title, rows, x, y, w, width, height, draw_header_fn, shaded=False):
    title_gap = 0.45 * cm
    pad_x = 0.6 * cm
    pad_y = 0.45 * cm
    row_h = 0.42 * cm

    idx = 0
    first = True

    while idx < len(rows):
        title_text = title if first else f"{title} (continuação)"
        y = _ensure_space(c, y, needed=2.2 * cm, width=width, height=height, draw_header_fn=draw_header_fn)

        c.setFont("Helvetica-Bold", 10)
        c.setFillColorRGB(0.06, 0.10, 0.18)
        c.drawString(x, y, title_text)
        y -= title_gap

        available_h = y - 2.2 * cm
        max_rows = max(1, int((available_h - (pad_y * 2)) / (row_h + 2)))
        chunk = rows[idx: idx + max_rows]
        idx += len(chunk)

        box_h = (pad_y * 2) + (len(chunk) * row_h) + 0.35 * cm
        if shaded:
            _draw_box(c, x, y, w, box_h, fill_rgb=(0.97, 0.98, 1.0), stroke_rgb=(0.97, 0.98, 1.0), stroke=0)
        else:
            _draw_box(c, x, y, w, box_h, fill_rgb=(1, 1, 1), stroke_rgb=(0.86, 0.89, 0.96), stroke=1)

        yy = y - pad_y - 0.10 * cm
        c.setFont("Helvetica", 9.2)
        c.setFillColorRGB(0.12, 0.16, 0.26)
        for ln in chunk:
            c.drawString(x + pad_x, yy, "• " + ln[:110])
            yy -= row_h

        y -= box_h + 0.5 * cm
        first = False

    return y


def _draw_items_section(c, title, rows, x, y, w, width, height, draw_header_fn):
    title_gap = 0.45 * cm
    pad_x = 0.6 * cm
    pad_y = 0.45 * cm
    row_h = 0.42 * cm
    header_h = 0.55 * cm

    idx = 0
    first = True

    while idx < len(rows):
        title_text = title if first else f"{title} (continuação)"
        y = _ensure_space(c, y, needed=2.6 * cm, width=width, height=height, draw_header_fn=draw_header_fn)

        c.setFont("Helvetica-Bold", 10)
        c.setFillColorRGB(0.06, 0.10, 0.18)
        c.drawString(x, y, title_text)
        y -= title_gap

        available_h = y - 2.2 * cm
        max_rows = max(1, int((available_h - (pad_y * 2) - header_h) / row_h))
        chunk = rows[idx: idx + max_rows]
        idx += len(chunk)

        box_h = (pad_y * 2) + header_h + (len(chunk) * row_h) + 0.25 * cm
        _draw_box(c, x, y, w, box_h, fill_rgb=(0.97, 0.98, 1.0), stroke_rgb=(0.97, 0.98, 1.0), stroke=0)

        c.setFillColorRGB(0.12, 0.16, 0.26)
        c.setFont("Helvetica-Bold", 9)
        c.drawString(x + pad_x, y - pad_y - 0.10 * cm, "Descrição")
        c.drawRightString(x + w - pad_x, y - pad_y - 0.10 * cm, "Total")

        yy = y - pad_y - header_h
        c.setFont("Helvetica", 9.5)
        for row in chunk:
            c.drawString(x + pad_x, yy, row["left"][:70])
            c.drawRightString(x + w - pad_x, yy, row["right"])
            yy -= row_h

        y -= box_h + 0.5 * cm
        first = False

    return y


# =========================
# PDF principal
# =========================
def generate_proposal_pdf(data: dict) -> bytes:
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    is_pro = bool(data.get("is_pro", False))

    # logo só no PRO
    logo_img = None
    if is_pro and data.get("logo_b64"):
        try:
            logo_bytes = base64.b64decode(data["logo_b64"])
            logo_img = ImageReader(io.BytesIO(logo_bytes))
        except Exception:
            logo_img = None

    company_name = _safe(data.get("company_name"))
    author_name = _safe(data.get("author_name"))
    phone = _safe(data.get("phone"))

    if is_pro:
        brand_title = company_name or author_name or "Orçamento"
        subtitle = phone
    else:
        brand_title = "PropoFlow"
        subtitle = "Orçamentos profissionais em minutos"

    public_id = str(data.get("public_id", "0001"))

    def draw_header():
        _draw_header(
            c,
            width,
            height,
            is_pro=is_pro,
            brand_title=brand_title,
            subtitle=subtitle,
            public_id=public_id,
            logo_img=logo_img
        )

    draw_header()

    x = 2 * cm
    w = width - 4 * cm
    y = height - 3.4 * cm

    # =========================
    # Dados principais
    # =========================
    y = _ensure_space(c, y, needed=3.2 * cm, width=width, height=height, draw_header_fn=draw_header)
    _draw_box(c, x, y, w, 3.0 * cm, fill_rgb=(0.97, 0.98, 1.0), stroke_rgb=(0.97, 0.98, 1.0), stroke=0)

    gen_dt = datetime.now().strftime("%d/%m/%Y %H:%M")
    emit = company_name or author_name or _safe(data.get("author_email"))
    emit_line = emit
    if phone:
        emit_line += f" • {phone}"

    c.setFillColorRGB(0.12, 0.16, 0.26)
    c.setFont("Helvetica", 8.8)
    c.drawString(x + 0.6 * cm, y - 0.55 * cm, f"Gerado em: {gen_dt}")
    c.drawRightString(x + w - 0.6 * cm, y - 0.55 * cm, f"Emitente: {emit_line}")

    client = _safe(data.get("client_name")) or "-"
    proj = _safe(data.get("project_name")) or "-"
    deadline = _safe(data.get("deadline")) or "-"

    try:
        total_cents = int(data.get("total_cents") or 0)
    except:
        total_cents = _parse_price_to_cents(data.get("price") or "")
    total_brl = _brl_from_cents(total_cents) if total_cents > 0 else (_safe(data.get("price")) or "-")

    # =========================
    # DADOS DO CLIENTE
    # =========================
    info_lines = [
        f"Cliente: {client}",
        f"Serviço: {proj}",
        f"Prazo: {deadline}",
    ]

    y = _draw_list_section(
        c,
        "Dados do Cliente",
        info_lines,
        x,
        y,
        w,
        width,
        height,
        draw_header,
        shaded=False
    )

    y = _ensure_space(c, y, needed=3 * cm, width=width, height=height, draw_header_fn=draw_header)

    # =========================
    # TOTAL PROFISSIONAL
    # =========================
    box_w = 7 * cm
    box_h = 2 * cm
    y -= 0.3 * cm
    box_x = x + w - box_w
    y -= 0.8 * cm
    box_y = y

    _draw_box(c, box_x, box_y, box_w, box_h, fill_rgb=(0.06, 0.10, 0.18), stroke=0)

    c.setFont("Helvetica-Bold", 9)
    c.setFillColorRGB(0.12, 0.16, 0.26)
    c.drawString(box_x, box_y + 0.4 * cm, "Valor total do projeto")

    c.setFillColorRGB(1, 1, 1)
    c.setFont("Helvetica", 9)
    c.drawString(box_x + 0.5 * cm, box_y - 0.7 * cm, "TOTAL")

    c.setFont("Helvetica-Bold", 16)
    c.drawRightString(box_x + box_w - 0.5 * cm, box_y - 0.7 * cm, total_brl)

    y -= box_h + 0.6 * cm

    # =========================
    # Descrição
    # =========================
    desc = _safe(data.get("description")) or "—"
    desc_lines = _wrap_lines(desc, w - 1.2 * cm, font="Helvetica", size=10)
    y = _draw_paragraph_section(c, "Descrição do serviço", desc_lines, x, y, w, width, height, draw_header)

    # =========================
    # TABELA DE ITENS PROFISSIONAL
    # =========================
    items = data.get("items") or []

    if items:
        rows = _normalize_items(items)
        y = _draw_items_section(
            c,
            "Itens do Orçamento",
            rows,
            x,
            y,
            w,
            width,
            height,
            draw_header
        )

        # Fundo do cabeçalho
        c.setFillColorRGB(0.9, 0.9, 0.9)
        c.rect(x, y - 5, w, 18, fill=1, stroke=0)

        # Cabeçalho
        c.setFillColorRGB(0, 0, 0)
        c.setFont("Helvetica-Bold", 9)

        c.drawString(x + 5, y, "Descrição")
        c.drawString(x + 250, y, "Qtd")
        c.drawString(x + 300, y, "Preço Unit.")
        c.drawString(x + 400, y, "Subtotal")

        y -= 18

        c.setFont("Helvetica", 9)

        for it in items:
            desc = _safe(it.get("description"))
            qty = _fmt_qty(it.get("qty") or 1)

            unit_price_cents = int(it.get("unit_price_cents") or 0)
            total_cents = int(it.get("line_total_cents") or 0)

            unit_price = _brl_from_cents(unit_price_cents)
            subtotal = _brl_from_cents(total_cents)

            # Linha
            desc_lines = _wrap_lines(desc, 230, font="Helvetica", size=9)

            start_y = y
            for line in desc_lines:
                c.drawString(x + 5, y, line)
                y -= 10
            c.drawString(x + 250, start_y, qty)
            c.drawString(x + 300, start_y, unit_price)
            c.drawString(x + 400, start_y, subtotal)

            # Linha separadora
            c.setStrokeColorRGB(0.85, 0.85, 0.85)
            c.line(x, y - 5, x + w, y - 5)

            y -= 18

            if y < 100:
                c.showPage()
                draw_header()
                y = height - 100

                # redesenha cabeçalho da tabela
                c.setFont("Helvetica-Bold", 11)
                c.drawString(x, y, "Itens do Orçamento")
                y -= 15

                c.setFillColorRGB(0.9, 0.9, 0.9)
                c.rect(x, y - 5, w, 18, fill=1, stroke=0)

                c.setFillColorRGB(0, 0, 0)
                c.setFont("Helvetica-Bold", 9)

                c.drawString(x + 5, y, "Descrição")
                c.drawString(x + 250, y, "Qtd")
                c.drawString(x + 300, y, "Preço Unit.")
                c.drawString(x + 400, y, "Subtotal")

                y -= 18
                c.setFont("Helvetica", 9)
    # =========================
    # Como pagar
    # =========================
    stage_rows = _normalize_stages(data.get("payment_stages") or [])
    if stage_rows:
        y = _draw_list_section(
            c,
            "Como pagar",
            stage_rows,
            x,
            y,
            w,
            width,
            height,
            draw_header,
            shaded=False
        )

    footer_y = 2.1 * cm

    # =========================
    # Condições
    # =========================
    terms_rows = _normalize_terms(data.get("payment_terms") or [])
    if terms_rows:
        y = _draw_list_section(c, "Observações", terms_rows, x, y, w, width, height, draw_header, shaded=True)


        # Assinatura
        c.setStrokeColorRGB(0.7, 0.7, 0.7)
        c.line(x, footer_y + 1.5 * cm, x + 6 * cm, footer_y + 1.5 * cm)

        c.setFont("Helvetica", 8)
        c.setFillColorRGB(0.3, 0.3, 0.3)
        c.drawString(x, footer_y + 1.2 * cm, "Assinatura do responsável")


    # =========================
    # Aceite online / Rodapé
    # =========================
    accept_url = _safe(data.get("accept_url"))

    if accept_url:
        c.setFont("Helvetica-Bold", 9.3)
        c.setFillColorRGB(0.06, 0.10, 0.18)
        c.drawString(x, footer_y + 0.25 * cm, "Aceite online:")
        c.setFont("Helvetica", 8.6)
        c.setFillColorRGB(0.12, 0.16, 0.26)
        c.drawString(x, footer_y - 0.15 * cm, accept_url[:110])

    if not is_pro:
        c.setFont("Helvetica", 8)
        c.setFillColorRGB(0.45, 0.50, 0.62)
        c.drawRightString(width - 2 * cm, 1.15 * cm, "Gerado com PropoFlow • Remova no PRO")

    c.save()
    buffer.seek(0)
    return buffer.getvalue()

