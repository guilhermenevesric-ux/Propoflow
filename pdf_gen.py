from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.units import cm
from reportlab.lib import colors
from datetime import datetime
from textwrap import wrap
import io


def _draw_header(c, width, height, title="PROPOSTA COMERCIAL", brand="PropoFlow", tagline="Propostas profissionais em minutos"):
    """
    Cabeçalho padrão.
    - brand: texto do lado esquerdo (marca)
    - tagline: frase menor abaixo da marca (pode ser "")
    """
    # faixa topo
    c.setFillColorRGB(0.06, 0.10, 0.18)
    c.rect(0, height - 3.2 * cm, width, 3.2 * cm, fill=1, stroke=0)

    # logo quadradinho (ícone)
    c.setFillColorRGB(0.31, 0.49, 1.00)
    c.roundRect(2 * cm, height - 2.45 * cm, 1.1 * cm, 1.1 * cm, 0.25 * cm, fill=1, stroke=0)

    # brand
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 13)
    c.drawString(3.4 * cm, height - 1.95 * cm, brand)

    # tagline (opcional)
    if tagline:
        c.setFont("Helvetica", 9)
        c.setFillColorRGB(0.85, 0.90, 1.00)
        c.drawString(3.4 * cm, height - 2.35 * cm, tagline)

    # título (direita)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 18)
    c.drawRightString(width - 2 * cm, height - 2.0 * cm, title)


def _section_title(c, x, y, text):
    c.setFont("Helvetica-Bold", 11)
    c.setFillColorRGB(0.06, 0.10, 0.18)
    c.drawString(x, y, text)


def _kv(c, x, y, k, v):
    c.setFont("Helvetica-Bold", 10)
    c.setFillColorRGB(0.12, 0.16, 0.26)
    c.drawString(x, y, k)
    c.setFont("Helvetica", 10)
    c.setFillColorRGB(0.12, 0.16, 0.26)
    c.drawString(x + 3.0 * cm, y, str(v) if v is not None else "")


def generate_proposal_pdf(data: dict) -> bytes:
    """
    data = {
      "client_name": "...",
      "project_name": "...",
      "description": "...",
      "price": "...",
      "deadline": "...",

      "author_email": "...",
      "author_name": "...",     # opcional
      "company_name": "...",    # opcional
      "phone": "...",           # opcional

      "is_pro": True/False      # NOVO (pro = white-label)
    }
    """
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    is_pro = bool(data.get("is_pro", False))

    # Marca no topo:
    # - PRO: usa company_name (ou author_name) e SEM frase PropoFlow
    # - FREE: usa PropoFlow e com tagline
    if is_pro:
        brand = (data.get("company_name") or data.get("author_name") or "Proposta").strip()
        tagline = ""  # sem tagline no PRO (white-label)
        footer_text = ""  # sem rodapé PropoFlow no PRO
    else:
        brand = "PropoFlow"
        tagline = "Propostas profissionais em minutos"
        footer_text = "Documento gerado automaticamente pelo PropoFlow."

    # Desenha cabeçalho 1x (correto)
    _draw_header(c, width, height, brand=brand, tagline=tagline)

    # corpo
    margin_x = 2 * cm
    y = height - 4.2 * cm

    # cartão de info
    c.setFillColorRGB(0.97, 0.98, 1.00)
    c.roundRect(margin_x, y - 2.6 * cm, width - 4 * cm, 2.6 * cm, 0.3 * cm, fill=1, stroke=0)

    c.setFillColorRGB(0.12, 0.16, 0.26)
    c.setFont("Helvetica", 9)
    c.drawString(margin_x + 0.6 * cm, y - 0.6 * cm, f"Gerado em: {datetime.now().strftime('%d/%m/%Y %H:%M')}")

    emitente = (data.get("company_name") or data.get("author_name") or data.get("author_email") or "").strip()
    contato = (data.get("phone") or "").strip()

    right_line = emitente
    if contato:
        right_line += f" • {contato}"

    c.drawRightString(width - 2.6 * cm, y - 0.6 * cm, f"Emitente: {right_line}")

    _kv(c, margin_x + 0.6 * cm, y - 1.4 * cm, "Cliente:", data.get("client_name", ""))
    _kv(c, margin_x + 0.6 * cm, y - 2.1 * cm, "Projeto:", data.get("project_name", ""))

    y = y - 3.4 * cm

    # seção escopo
    _section_title(c, margin_x, y, "ESCOPO / DESCRIÇÃO")
    y -= 0.6 * cm

    c.setFont("Helvetica", 10)
    c.setFillColorRGB(0.12, 0.16, 0.26)
    lines = wrap(data.get("description", ""), width=95)

    for line in lines:
        c.drawString(margin_x, y, line)
        y -= 0.48 * cm

        # quebra de página
        if y < 5.0 * cm:
            c.showPage()
            _draw_header(c, width, height, brand=brand, tagline=tagline)
            y = height - 4.2 * cm
            c.setFillColorRGB(0.12, 0.16, 0.26)
            c.setFont("Helvetica", 10)

    y -= 0.4 * cm

    # bloco investimento/prazo
    c.setFillColorRGB(0.97, 0.98, 1.00)
    c.roundRect(margin_x, y - 2.0 * cm, width - 4 * cm, 2.0 * cm, 0.3 * cm, fill=1, stroke=0)

    c.setFillColorRGB(0.12, 0.16, 0.26)
    _section_title(c, margin_x + 0.6 * cm, y - 0.6 * cm, "INVESTIMENTO E PRAZO")

    c.setFont("Helvetica-Bold", 12)
    c.setFillColorRGB(0.06, 0.10, 0.18)
    c.drawString(margin_x + 0.6 * cm, y - 1.35 * cm, f"Valor: {data.get('price', '')}")

    c.setFont("Helvetica", 11)
    c.setFillColorRGB(0.12, 0.16, 0.26)
    c.drawRightString(width - 2.6 * cm, y - 1.35 * cm, f"Prazo: {data.get('deadline', '')}")

    y -= 2.6 * cm

    # condições
    _section_title(c, margin_x, y, "CONDIÇÕES")
    y -= 0.6 * cm

    c.setFont("Helvetica", 10)
    c.setFillColorRGB(0.12, 0.16, 0.26)
    cond = [
        "• Validade: 7 dias a partir da data de emissão.",
        "• Pagamento: 50% na aprovação e 50% na entrega (ajustável).",
        "• Alterações fora do escopo podem ser orçadas separadamente.",
    ]
    for item in cond:
        c.drawString(margin_x, y, item)
        y -= 0.5 * cm

    y -= 0.8 * cm

    # assinatura
    c.setStrokeColorRGB(0.75, 0.80, 0.90)
    c.line(margin_x, y, margin_x + 8 * cm, y)

    c.setFont("Helvetica", 10)
    c.setFillColorRGB(0.12, 0.16, 0.26)
    c.drawString(margin_x, y - 0.45 * cm, "Assinatura / Responsável")

    # rodapé (só FREE)
    if footer_text:
        c.setFont("Helvetica", 8)
        c.setFillColorRGB(0.45, 0.50, 0.62)
        c.drawString(margin_x, 1.5 * cm, footer_text)

    c.showPage()
    c.save()

    buffer.seek(0)
    return buffer.getvalue()