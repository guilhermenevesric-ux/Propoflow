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
    Tenta converter "R$ 1.234,56" / "1234,56" / "1234.56" / "1234" em centavos.
    """
    s = _safe(price_str)
    if not s:
        return 0
    s = s.lower().replace("r$", "").strip()
    s = re.sub(r"[^\d,\.]", "", s)
    if not s:
        return 0
    s = s.replace(".", "").replace(",", ".")
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


def _wrap_draw(c, text, x, y, max_w, font="Helvetica", size=10, leading=12.5, color=(0.12, 0.16, 0.26)):
    c.setFont(font, size)
    c.setFillColorRGB(*color)
    words = (text or "").split()
    if not words:
        return y

    line = ""
    for w in words:
        test = (line + " " + w).strip()
        if stringWidth(test, font, size) <= max_w:
            line = test
        else:
            if line:
                c.drawString(x, y, line)
                y -= leading
            line = w

    if line:
        c.drawString(x, y, line)
        y -= leading
    return y


def _ensure_space(c, y, needed, width, height, draw_header_fn):
    if y - needed < 2.2 * cm:
        c.showPage()
        draw_header_fn()
        return height - 3.4 * cm
    return y


# =========================
# Header
# =========================
def _draw_header(c, width, height, is_pro: bool, brand_title: str, subtitle: str, logo_img=None):
    # barra topo
    c.setFillColorRGB(0.06, 0.10, 0.18)
    c.roundRect(2 * cm, height - 2.55 * cm, width - 4 * cm, 1.75 * cm, 0.28 * cm, fill=1, stroke=0)

    # logo / ícone
    if is_pro and logo_img is not None:
        try:
            c.drawImage(logo_img, 2.35 * cm, height - 2.25 * cm, 1.05 * cm, 1.05 * cm, mask="auto")
        except Exception:
            c.setFillColorRGB(0.31, 0.49, 1.00)
            c.roundRect(2.35 * cm, height - 2.15 * cm, 0.95 * cm, 0.95 * cm, 0.24 * cm, fill=1, stroke=0)
    else:
        c.setFillColorRGB(0.31, 0.49, 1.00)
        c.roundRect(2.35 * cm, height - 2.15 * cm, 0.95 * cm, 0.95 * cm, 0.24 * cm, fill=1, stroke=0)

    # título esquerdo
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 11.5)
    c.drawString(3.55 * cm, height - 1.88 * cm, brand_title)

    c.setFont("Helvetica", 8.5)
    c.setFillColorRGB(0.86, 0.90, 1.00)
    if subtitle:
        c.drawString(3.55 * cm, height - 2.20 * cm, subtitle)

    # título direita
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 14)
    c.drawRightString(width - 2.35 * cm, height - 1.90 * cm, "ORÇAMENTO")


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

    # marca
    company_name = _safe(data.get("company_name"))
    author_name = _safe(data.get("author_name"))
    phone = _safe(data.get("phone"))

    if is_pro:
        brand_title = company_name or author_name or "Orçamento"
        subtitle = phone
    else:
        brand_title = "PropoFlow"
        subtitle = "Orçamentos profissionais em minutos"

    def draw_header():
        _draw_header(c, width, height, is_pro=is_pro, brand_title=brand_title, subtitle=subtitle, logo_img=logo_img)

    draw_header()

    x = 2 * cm
    w = width - 4 * cm
    y = height - 3.4 * cm

    # Dados
    y = _ensure_space(c, y, needed=3.2 * cm, width=width, height=height, draw_header_fn=draw_header)
    c.setFillColorRGB(0.97, 0.98, 1.0)
    c.roundRect(x, y - 3.0 * cm, w, 3.0 * cm, 0.25 * cm, fill=1, stroke=0)

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

    # total
    total_cents = int(data.get("total_cents") or 0)
    if total_cents <= 0:
        total_cents = _parse_price_to_cents(data.get("price") or "")
    total_brl = _brl_from_cents(total_cents) if total_cents > 0 else (_safe(data.get("price")) or "-")

    c.setFont("Helvetica-Bold", 10)
    c.drawString(x + 0.6 * cm, y - 1.35 * cm, "Cliente")
    c.setFont("Helvetica", 10)
    c.drawString(x + 0.6 * cm, y - 1.85 * cm, client)

    c.setFont("Helvetica-Bold", 10)
    c.drawString(x + 0.6 * cm, y - 2.40 * cm, "Serviço")
    c.setFont("Helvetica", 10)
    c.drawString(x + 0.6 * cm, y - 2.90 * cm, proj[:60])

    c.setFont("Helvetica-Bold", 10)
    c.drawRightString(x + w - 0.6 * cm, y - 1.35 * cm, "Prazo")
    c.setFont("Helvetica", 10)
    c.drawRightString(x + w - 0.6 * cm, y - 1.85 * cm, deadline)

    c.setFont("Helvetica-Bold", 10)
    c.drawRightString(x + w - 0.6 * cm, y - 2.40 * cm, "Total")
    c.setFont("Helvetica-Bold", 14)
    c.drawRightString(x + w - 0.6 * cm, y - 2.95 * cm, total_brl)

    y -= 3.6 * cm

    # Descrição
    desc = _safe(data.get("description"))
    y = _ensure_space(c, y, needed=2.9 * cm, width=width, height=height, draw_header_fn=draw_header)

    c.setFont("Helvetica-Bold", 10)
    c.setFillColorRGB(0.06, 0.10, 0.18)
    c.drawString(x, y, "Descrição do serviço")
    y -= 0.45 * cm

    c.setFillColorRGB(1, 1, 1)
    c.setStrokeColorRGB(0.86, 0.89, 0.96)
    c.roundRect(x, y - 2.35 * cm, w, 2.35 * cm, 0.25 * cm, fill=1, stroke=1)

    y_text = y - 0.55 * cm
    y_text = _wrap_draw(c, desc or "—", x + 0.6 * cm, y_text, w - 1.2 * cm, size=10, leading=12.5)
    y -= 2.85 * cm

    # Itens
    items = data.get("items") or []
    if isinstance(items, list) and len(items) > 0:
        y = _ensure_space(c, y, needed=3.4 * cm, width=width, height=height, draw_header_fn=draw_header)
        c.setFont("Helvetica-Bold", 10)
        c.setFillColorRGB(0.06, 0.10, 0.18)
        c.drawString(x, y, "Itens")
        y -= 0.45 * cm

        c.setFillColorRGB(0.97, 0.98, 1.0)
        c.roundRect(x, y - 2.4 * cm, w, 2.4 * cm, 0.25 * cm, fill=1, stroke=0)

        c.setFillColorRGB(0.12, 0.16, 0.26)
        c.setFont("Helvetica-Bold", 9)
        c.drawString(x + 0.6 * cm, y - 0.55 * cm, "Descrição")
        c.drawRightString(x + w - 0.6 * cm, y - 0.55 * cm, "Total")

        yy = y - 1.05 * cm
        c.setFont("Helvetica", 9.5)

        for it in items[:6]:
            d = _safe(it.get("description") or it.get("desc"))
            qty = _fmt_qty(it.get("qty") or 1)
            unit_price_cents = int(it.get("unit_price_cents") or it.get("unit_price") or 0)
            line_total_cents = int(it.get("line_total_cents") or 0)

            if line_total_cents <= 0:
                try:
                    line_total_cents = int(round(float(qty) * unit_price_cents))
                except Exception:
                    line_total_cents = 0

            left = f"{d} ({qty}x)"
            right = _brl_from_cents(line_total_cents) if line_total_cents > 0 else "—"

            c.drawString(x + 0.6 * cm, yy, left[:65])
            c.drawRightString(x + w - 0.6 * cm, yy, right)
            yy -= 0.42 * cm
            if yy < y - 2.1 * cm:
                break

        y -= 2.85 * cm

    # Como pagar
    stages = data.get("payment_stages") or []
    if isinstance(stages, list) and len(stages) > 0:
        y = _ensure_space(c, y, needed=2.2 * cm, width=width, height=height, draw_header_fn=draw_header)
        c.setFont("Helvetica-Bold", 10)
        c.setFillColorRGB(0.06, 0.10, 0.18)
        c.drawString(x, y, "Como pagar")
        y -= 0.45 * cm

        c.setFillColorRGB(1, 1, 1)
        c.setStrokeColorRGB(0.86, 0.89, 0.96)
        c.roundRect(x, y - 1.35 * cm, w, 1.35 * cm, 0.25 * cm, fill=1, stroke=1)

        yy = y - 0.55 * cm
        c.setFont("Helvetica", 9.5)
        c.setFillColorRGB(0.12, 0.16, 0.26)

        for st in stages[:4]:
            title = _safe(st.get("title"))
            amt = _brl_from_cents(int(st.get("amount_cents") or 0))
            pct = st.get("percent")
            if pct is not None:
                ln = f"{title}: {amt} ({pct}%)"
            else:
                ln = f"{title}: {amt}"
            c.drawString(x + 0.6 * cm, yy, "• " + ln[:95])
            yy -= 0.42 * cm

        y -= 1.85 * cm

    # Condições
    terms = data.get("payment_terms") or []
    if isinstance(terms, list) and len(terms) > 0:
        y = _ensure_space(c, y, needed=2.2 * cm, width=width, height=height, draw_header_fn=draw_header)
        c.setFont("Helvetica-Bold", 10)
        c.setFillColorRGB(0.06, 0.10, 0.18)
        c.drawString(x, y, "Condições")
        y -= 0.45 * cm

        c.setFillColorRGB(0.97, 0.98, 1.0)
        c.roundRect(x, y - 1.5 * cm, w, 1.5 * cm, 0.25 * cm, fill=1, stroke=0)

        yy = y - 0.55 * cm
        c.setFont("Helvetica", 9.2)
        c.setFillColorRGB(0.12, 0.16, 0.26)

        for ln in terms[:4]:
            c.drawString(x + 0.6 * cm, yy, "• " + _safe(ln)[:95])
            yy -= 0.40 * cm

        y -= 1.85 * cm

    # Aceite online
    accept_url = _safe(data.get("accept_url"))
    if accept_url:
        c.setFont("Helvetica-Bold", 9.3)
        c.setFillColorRGB(0.06, 0.10, 0.18)
        c.drawString(x, 2.35 * cm, "Aceite online:")
        c.setFont("Helvetica", 8.6)
        c.setFillColorRGB(0.12, 0.16, 0.26)
        c.drawString(x, 1.95 * cm, accept_url[:110])

    # Rodapé (apenas FREE)
    if not is_pro:
        c.setFont("Helvetica", 8)
        c.setFillColorRGB(0.45, 0.50, 0.62)
        c.drawRightString(width - 2 * cm, 1.15 * cm, "Gerado com PropoFlow • Remova no PRO")

    c.showPage()
    c.save()

    buffer.seek(0)
    return buffer.getvalue()