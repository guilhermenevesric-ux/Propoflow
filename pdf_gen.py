from reportlab.lib.utils import ImageReader
import base64
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.pdfbase.pdfmetrics import stringWidth
from datetime import datetime
import io
import re

def _safe(x):
    return (x or "").strip()

def _brl_from_cents(cents: int) -> str:
    n = max(0, int(cents)) / 100.0
    return f"R$ {n:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

def _brl(value):
    if value is None:
        return ""
    s = str(value).strip()
    if not s:
        return ""
    if "r$" in s.lower():
        digits = re.sub(r"[^\d,\.]", "", s)
        if digits:
            tmp = digits.replace(".", "").replace(",", ".")
            try:
                n = float(tmp)
                return f"R$ {n:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
            except Exception:
                return s
        return s
    digits = re.sub(r"[^\d,\.]", "", s)
    if not digits:
        return s
    tmp = digits.replace(".", "").replace(",", ".")
    try:
        n = float(tmp)
        return f"R$ {n:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except Exception:
        return s

def _draw_header(c, width, height, brand, tagline, doc_title="PROPOSTA COMERCIAL", logo_img=None):
    # ícone / logo
    if logo_img:
        try:
            c.drawImage(logo_img, 2 * cm, height - 2.75 * cm, 1.6 * cm, 1.6 * cm, mask='auto')
        except Exception:
            c.setFillColorRGB(0.31, 0.49, 1.00)
            c.roundRect(2 * cm, height - 2.55 * cm, 1.15 * cm, 1.15 * cm, 0.28 * cm, fill=1, stroke=0)
    else:
        c.setFillColorRGB(0.31, 0.49, 1.00)
        c.roundRect(2 * cm, height - 2.55 * cm, 1.15 * cm, 1.15 * cm, 0.28 * cm, fill=1, stroke=0)

def _wrap_draw(c, text, x, y, max_w, font="Helvetica", size=10, leading=13, color=(0.12, 0.16, 0.26)):
    c.setFont(font, size)
    c.setFillColorRGB(*color)
    words = (text or "").split()
    if not words:
        return y
    line = ""
    lines = []
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
    for ln in lines:
        c.drawString(x, y, ln)
        y -= leading * 0.75
    return y

def _ensure_space(c, y, needed, width, height, brand, tagline):
    if y - needed < 3.0 * cm:
        c.showPage()
        _draw_header(c, width, height, brand, tagline)
        return height - 4.1 * cm
    return y

def generate_proposal_pdf(data: dict) -> bytes:
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    is_pro = bool(data.get("is_pro", False))
    if is_pro:
        brand = (_safe(data.get("company_name")) or _safe(data.get("author_name")) or "Orçamento")
        tagline = ""
        footer_text = ""
    else:
        brand = "PropoFlow"
        tagline = "Orçamentos profissionais em minutos"
        footer_text = "Documento gerado automaticamente pelo PropoFlow."

    logo_b64 = data.get("logo_b64")
    logo_mime = data.get("logo_mime")
    logo_img = None
    if logo_b64:
        try:
            logo_bytes = base64.b64decode(logo_b64)
            logo_img = ImageReader(io.BytesIO(logo_bytes))
        except Exception:
            logo_img = None

    _draw_header(c, width, height, brand=brand, tagline=tagline, logo_img=logo_img)

    margin_x = 2 * cm
    content_w = width - 4 * cm
    y = height - 4.1 * cm

    gen_dt = datetime.now().strftime("%d/%m/%Y %H:%M")
    emitente = (_safe(data.get("company_name")) or _safe(data.get("author_name")) or _safe(data.get("author_email")))
    contato = _safe(data.get("phone"))
    emit_line = emitente + (f" • {contato}" if contato else "")

    c.setFont("Helvetica", 9)
    c.setFillColorRGB(0.12, 0.16, 0.26)
    c.drawString(margin_x, y, f"Gerado em: {gen_dt}")
    c.drawRightString(width - margin_x, y, f"Emitente: {emit_line}")
    y -= 0.7 * cm

    c.setFont("Helvetica-Bold", 11)
    c.setFillColorRGB(0.06, 0.10, 0.18)
    c.drawString(margin_x, y, "DADOS")
    y -= 0.5 * cm

    y = _wrap_draw(c, f"Cliente: {_safe(data.get('client_name')) or '-'}", margin_x, y, content_w)
    y = _wrap_draw(c, f"Projeto: {_safe(data.get('project_name')) or '-'}", margin_x, y, content_w)
    y = _wrap_draw(c, f"Prazo: {_safe(data.get('deadline')) or '-'}", margin_x, y, content_w)
    y -= 0.3 * cm

    c.setFont("Helvetica-Bold", 11)
    c.setFillColorRGB(0.06, 0.10, 0.18)
    c.drawString(margin_x, y, "O QUE SERÁ FEITO")
    y -= 0.55 * cm

    y = _ensure_space(c, y, 2.0 * cm, width, height, brand, tagline)
    y = _wrap_draw(c, _safe(data.get("description")) or "—", margin_x, y, content_w)
    y -= 0.3 * cm

    items = data.get("items") or []
    if isinstance(items, list) and items:
        y = _ensure_space(c, y, 3.0 * cm, width, height, brand, tagline)
        c.setFont("Helvetica-Bold", 11)
        c.setFillColorRGB(0.06, 0.10, 0.18)
        c.drawString(margin_x, y, "ITENS")
        y -= 0.6 * cm

        c.setFont("Helvetica-Bold", 9)
        c.setFillColorRGB(0.12, 0.16, 0.26)
        c.drawString(margin_x, y, "Descrição")
        c.drawRightString(width - margin_x, y, "Total")
        y -= 0.35 * cm
        c.setStrokeColorRGB(0.86, 0.89, 0.96)
        c.line(margin_x, y, width - margin_x, y)
        y -= 0.35 * cm

        c.setFont("Helvetica", 9)
        for it in items[:22]:
            y = _ensure_space(c, y, 0.8 * cm, width, height, brand, tagline)
            desc = f"{it.get('description','')} ({it.get('qty',1)} {it.get('unit','')}) x {_brl_from_cents(int(it.get('unit_price_cents',0)))}"
            total = _brl_from_cents(int(it.get("line_total_cents", 0)))
            y = _wrap_draw(c, desc, margin_x, y, content_w - 3.0 * cm, size=9)
            c.drawRightString(width - margin_x, y + 0.25 * cm, total)
            y -= 0.15 * cm

        y -= 0.2 * cm

    total_cents = int(data.get("total_cents") or 0)
    total_str = _brl_from_cents(total_cents) if total_cents > 0 else (_brl(data.get("price")) or "—")

    y = _ensure_space(c, y, 2.0 * cm, width, height, brand, tagline)
    c.setFont("Helvetica-Bold", 11)
    c.setFillColorRGB(0.06, 0.10, 0.18)
    c.drawString(margin_x, y, "TOTAL")
    y -= 0.6 * cm

    c.setFillColorRGB(0.06, 0.10, 0.18)
    c.roundRect(margin_x, y - 1.2 * cm, content_w, 1.2 * cm, 0.25 * cm, fill=1, stroke=0)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 20)
    c.drawString(margin_x + 0.6 * cm, y - 0.85 * cm, total_str)
    y -= 1.6 * cm

    stages = data.get("payment_stages") or []
    if isinstance(stages, list) and stages:
        y = _ensure_space(c, y, 2.4 * cm, width, height, brand, tagline)
        c.setFont("Helvetica-Bold", 11)
        c.setFillColorRGB(0.06, 0.10, 0.18)
        c.drawString(margin_x, y, "COMO PAGAR")
        y -= 0.6 * cm
        c.setFont("Helvetica", 10)
        c.setFillColorRGB(0.12, 0.16, 0.26)
        for st in stages[:6]:
            line = f"- {st.get('title','Etapa')}: {_brl_from_cents(int(st.get('amount_cents',0)))} ({int(st.get('percent',0))}%)"
            y = _wrap_draw(c, line, margin_x, y, content_w, size=10)
        y -= 0.2 * cm

    accept_url = _safe(data.get("accept_url"))
    if accept_url:
        y = _ensure_space(c, y, 1.6 * cm, width, height, brand, tagline)
        c.setFont("Helvetica-Bold", 11)
        c.setFillColorRGB(0.06, 0.10, 0.18)
        c.drawString(margin_x, y, "ACEITE")
        y -= 0.55 * cm
        y = _wrap_draw(c, f"Aceite online: {accept_url}", margin_x, y, content_w, size=9)

    if footer_text:
        c.setFont("Helvetica", 8)
        c.setFillColorRGB(0.45, 0.50, 0.62)
        c.drawString(margin_x, 1.5 * cm, footer_text)

    c.showPage()
    c.save()

    buffer.seek(0)
    return buffer.getvalue()