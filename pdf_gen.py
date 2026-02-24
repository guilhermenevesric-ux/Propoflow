# pdf_gen.py
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.pdfbase.pdfmetrics import stringWidth
from datetime import datetime
from textwrap import wrap
import io
import re


# =========================
# Helpers de formatação
# =========================
def _brl(value):
    """
    Tenta formatar valores tipo:
      "19" -> "R$ 19,00"
      "19.9" -> "R$ 19,90"
      "R$ 19,90" -> "R$ 19,90"
      "19,90" -> "R$ 19,90"
      "R$ 1.500" -> "R$ 1.500,00"
    Se não der, devolve o original.
    """
    if value is None:
        return ""
    s = str(value).strip()
    if not s:
        return ""

    # já tem R$
    if "r$" in s.lower():
        # tenta normalizar "R$ 1500" -> "R$ 1.500,00"
        digits = re.sub(r"[^\d,\.]", "", s)
        if digits:
            # troca separador decimal para ponto e parseia
            tmp = digits.replace(".", "").replace(",", ".")
            try:
                n = float(tmp)
                return f"R$ {n:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
            except Exception:
                return s
        return s

    # remove qualquer coisa que não seja número / , / .
    digits = re.sub(r"[^\d,\.]", "", s)
    if not digits:
        return s

    # heurística BR: se tem vírgula, ela é decimal; se tem ponto e vírgula, assume ponto milhar
    tmp = digits.replace(".", "").replace(",", ".")
    try:
        n = float(tmp)
        return f"R$ {n:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except Exception:
        return s


def _format_deadline(deadline):
    """
    Se vier só "1" ou "7", tenta virar "1 dia" / "7 dias".
    Se já vier com texto, mantém.
    """
    if deadline is None:
        return ""
    s = str(deadline).strip()
    if not s:
        return ""

    # se for número puro
    if re.fullmatch(r"\d+", s):
        n = int(s)
        return f"{n} dia" if n == 1 else f"{n} dias"

    return s


def _safe(text):
    return (text or "").strip()


def _extract_bullets(text):
    """
    Extrai bullets do texto:
    - linhas começando com -, •, *, ou "1)"/"1." viram bullets
    - se não tiver nada, retorna lista vazia
    """
    if not text:
        return []
    lines = [l.strip() for l in str(text).splitlines() if l.strip()]
    bullets = []
    for l in lines:
        if re.match(r"^(\-|\•|\*|\d+\)|\d+\.)\s+", l):
            l2 = re.sub(r"^(\-|\•|\*|\d+\)|\d+\.)\s+", "", l).strip()
            if l2:
                bullets.append(l2)
    return bullets


def _auto_deliverables(project_name, description):
    """
    Se o usuário não colocou deliverables, tenta:
    - usar bullets da descrição
    - senão, cria 4 entregáveis genéricos (melhor do que ficar vazio)
    """
    bullets = _extract_bullets(description)
    if bullets:
        return bullets[:8]

    pn = _safe(project_name) or "Projeto"
    return [
        f"Diagnóstico e alinhamento do {pn}",
        "Entrega do escopo acordado (com checklist)",
        "1 rodada de ajustes dentro do escopo",
        "Entrega final + orientações para próximos passos",
    ]


def _auto_timeline(deadline):
    """
    Um cronograma simples para não ficar vazio.
    Se o prazo for curto, usa 3 etapas; se for longo, 4.
    """
    d = _safe(deadline)
    # tenta extrair um número (dias) se houver
    m = re.search(r"(\d+)", d)
    days = int(m.group(1)) if m else None

    if days is not None and days <= 3:
        return [
            ("Etapa 1", "Alinhamento + coleta de informações"),
            ("Etapa 2", "Execução e prévia"),
            ("Etapa 3", "Ajustes finais e entrega"),
        ]
    return [
        ("Etapa 1", "Alinhamento + coleta de informações"),
        ("Etapa 2", "Execução do escopo"),
        ("Etapa 3", "Prévia / validação"),
        ("Etapa 4", "Ajustes finais e entrega"),
    ]


# =========================
# UI (ReportLab)
# =========================
def _draw_header(c, width, height, brand, tagline, doc_title="PROPOSTA COMERCIAL"):
    # faixa topo
    c.setFillColorRGB(0.06, 0.10, 0.18)
    c.rect(0, height - 3.3 * cm, width, 3.3 * cm, fill=1, stroke=0)

    # ícone
    c.setFillColorRGB(0.31, 0.49, 1.00)
    c.roundRect(2 * cm, height - 2.55 * cm, 1.15 * cm, 1.15 * cm, 0.28 * cm, fill=1, stroke=0)

    # brand
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 13.5)
    c.drawString(3.45 * cm, height - 2.02 * cm, brand)

    if tagline:
        c.setFont("Helvetica", 9)
        c.setFillColorRGB(0.86, 0.90, 1.00)
        c.drawString(3.45 * cm, height - 2.42 * cm, tagline)

    # título direita
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 18)
    c.drawRightString(width - 2 * cm, height - 2.05 * cm, doc_title)


def _card(c, x, y_top, w, h, radius=0.32 * cm, fill=(0.97, 0.98, 1.0), stroke=None):
    if fill:
        c.setFillColorRGB(*fill)
    if stroke:
        c.setStrokeColorRGB(*stroke)
    else:
        c.setStrokeColor(colors.transparent)
    c.roundRect(x, y_top - h, w, h, radius, fill=1, stroke=1 if stroke else 0)


def _section_title(c, x, y, text):
    c.setFont("Helvetica-Bold", 10.8)
    c.setFillColorRGB(0.06, 0.10, 0.18)
    c.drawString(x, y, text)


def _kv(c, x, y, k, v, key_w=2.8 * cm):
    c.setFont("Helvetica-Bold", 10)
    c.setFillColorRGB(0.12, 0.16, 0.26)
    c.drawString(x, y, k)
    c.setFont("Helvetica", 10)
    c.setFillColorRGB(0.12, 0.16, 0.26)
    c.drawString(x + key_w, y, v)


def _wrap_draw(c, text, x, y, max_w, font="Helvetica", size=10, leading=13, color=(0.12, 0.16, 0.26)):
    """
    Desenha texto com quebra automática por largura real (stringWidth).
    Retorna o novo y após desenhar.
    """
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
        y -= leading * 0.75  # ajusta para ficar mais “apertado” e preencher melhor
    return y


def _ensure_space(c, y, needed, width, height, brand, tagline):
    """
    Se não houver espaço suficiente, cria nova página com cabeçalho e retorna y resetado.
    """
    if y - needed < 3.0 * cm:
        c.showPage()
        _draw_header(c, width, height, brand, tagline)
        return height - 4.1 * cm
    return y


def _bullets(c, items, x, y, max_w, bullet="•", font="Helvetica", size=10, leading=13):
    c.setFont(font, size)
    c.setFillColorRGB(0.12, 0.16, 0.26)

    for it in items:
        it = _safe(it)
        if not it:
            continue
        # desenha bullet + texto quebrado
        c.drawString(x, y, bullet)
        y2 = _wrap_draw(c, it, x + 0.45 * cm, y, max_w - 0.45 * cm, font=font, size=size, leading=leading)
        y = y2 - 0.25 * cm
    return y


# =========================
# PDF principal
# =========================
def generate_proposal_pdf(data: dict) -> bytes:
    """
    data esperado (mantém compatibilidade com seu app.py):
      client_name, project_name, description, price, deadline,
      author_email, author_name, company_name, phone,
      is_pro

    (Opcional, se no futuro você quiser evoluir sem quebrar nada):
      deliverables: list[str]
      timeline: list[tuple(str, str)]  # (Etapa, Descrição)
      payment_terms: list[str]
      validity_days: int
      accept_url: str
    """
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    is_pro = bool(data.get("is_pro", False))

    # Marca (white-label no PRO)
    if is_pro:
        brand = (_safe(data.get("company_name")) or _safe(data.get("author_name")) or "Proposta")
        tagline = ""
        footer_text = ""
    else:
        brand = "PropoFlow"
        tagline = "Propostas profissionais em minutos"
        footer_text = "Documento gerado automaticamente pelo PropoFlow."

    _draw_header(c, width, height, brand=brand, tagline=tagline)

    margin_x = 2 * cm
    content_w = width - 4 * cm
    y = height - 4.1 * cm

    # =========================
    # Card: Resumo / Identificação
    # =========================
    y = _ensure_space(c, y, needed=3.2 * cm, width=width, height=height, brand=brand, tagline=tagline)

    _card(c, margin_x, y, content_w, 3.05 * cm, fill=(0.97, 0.98, 1.0))

    gen_dt = datetime.now().strftime("%d/%m/%Y %H:%M")
    emitente = (_safe(data.get("company_name")) or _safe(data.get("author_name")) or _safe(data.get("author_email")))
    contato = _safe(data.get("phone"))
    emit_line = emitente
    if contato:
        emit_line += f" • {contato}"

    c.setFont("Helvetica", 9)
    c.setFillColorRGB(0.12, 0.16, 0.26)
    c.drawString(margin_x + 0.65 * cm, y - 0.7 * cm, f"Gerado em: {gen_dt}")
    c.drawRightString(width - 2.65 * cm, y - 0.7 * cm, f"Emitente: {emit_line}")

    _kv(c, margin_x + 0.65 * cm, y - 1.55 * cm, "Cliente:", _safe(data.get("client_name")) or "-")
    _kv(c, margin_x + 0.65 * cm, y - 2.30 * cm, "Projeto:", _safe(data.get("project_name")) or "-")

    y -= 3.55 * cm

    # =========================
    # Seção: Escopo (texto)
    # =========================
    desc = _safe(data.get("description"))
    y = _ensure_space(c, y, needed=2.2 * cm, width=width, height=height, brand=brand, tagline=tagline)

    _section_title(c, margin_x, y, "ESCOPO / CONTEXTO")
    y -= 0.55 * cm

    # mesmo se vier curto, a gente “encaixa” em um bloco para não ficar vazio solto
    scope_h = 3.2 * cm
    _card(c, margin_x, y, content_w, scope_h, fill=(1.0, 1.0, 1.0), stroke=(0.86, 0.89, 0.96))
    y_text = y - 0.65 * cm
    y_text = _wrap_draw(c, desc if desc else "—", margin_x + 0.65 * cm, y_text, content_w - 1.3 * cm, size=10, leading=13)
    y -= (scope_h + 0.55 * cm)

    # =========================
    # Seção: Entregáveis (PREENCHE O VAZIO e dá cara de agência)
    # =========================
    deliverables = data.get("deliverables")
    if not isinstance(deliverables, list) or not deliverables:
        deliverables = _auto_deliverables(data.get("project_name"), data.get("description"))

    y = _ensure_space(c, y, needed=3.4 * cm, width=width, height=height, brand=brand, tagline=tagline)
    _section_title(c, margin_x, y, "ENTREGÁVEIS")
    y -= 0.55 * cm

    deliv_h = 3.6 * cm
    _card(c, margin_x, y, content_w, deliv_h, fill=(0.97, 0.98, 1.0))
    y_list = y - 0.75 * cm
    y_list = _bullets(c, deliverables[:8], margin_x + 0.65 * cm, y_list, content_w - 1.3 * cm)
    y -= (deliv_h + 0.65 * cm)

    # =========================
    # Seção: Investimento e Prazo (com destaque)
    # =========================
    price = _brl(data.get("price"))
    deadline = _format_deadline(data.get("deadline"))

    y = _ensure_space(c, y, needed=2.9 * cm, width=width, height=height, brand=brand, tagline=tagline)
    _section_title(c, margin_x, y, "INVESTIMENTO E PRAZO")
    y -= 0.55 * cm

    inv_h = 2.75 * cm
    _card(c, margin_x, y, content_w, inv_h, fill=(0.06, 0.10, 0.18))
    # Valor (grande)
    c.setFillColor(colors.white)
    c.setFont("Helvetica", 9)
    c.drawString(margin_x + 0.65 * cm, y - 0.65 * cm, "Valor")
    c.setFont("Helvetica-Bold", 20)
    c.drawString(margin_x + 0.65 * cm, y - 1.55 * cm, price or "—")

    # Prazo (direita)
    c.setFont("Helvetica", 9)
    c.drawRightString(width - 2.65 * cm, y - 0.65 * cm, "Prazo")
    c.setFont("Helvetica-Bold", 14)
    c.drawRightString(width - 2.65 * cm, y - 1.52 * cm, deadline or "—")

    y -= (inv_h + 0.75 * cm)

    # =========================
    # Seção: Cronograma (mais cara de projeto)
    # =========================
    timeline = data.get("timeline")
    if not isinstance(timeline, list) or not timeline:
        timeline = _auto_timeline(deadline)

    y = _ensure_space(c, y, needed=3.2 * cm, width=width, height=height, brand=brand, tagline=tagline)
    _section_title(c, margin_x, y, "CRONOGRAMA (RESUMO)")
    y -= 0.55 * cm

    cron_h = 3.35 * cm
    _card(c, margin_x, y, content_w, cron_h, fill=(1.0, 1.0, 1.0), stroke=(0.86, 0.89, 0.96))

    y_row = y - 0.75 * cm
    x1 = margin_x + 0.65 * cm
    x2 = margin_x + 4.2 * cm
    max_w = content_w - (x2 - margin_x) - 0.65 * cm

    for i, item in enumerate(timeline[:4]):
        etapa, desc_etapa = item if isinstance(item, (list, tuple)) and len(item) >= 2 else (f"Etapa {i+1}", str(item))
        c.setFont("Helvetica-Bold", 10)
        c.setFillColorRGB(0.06, 0.10, 0.18)
        c.drawString(x1, y_row, str(etapa))
        y_row = _wrap_draw(c, str(desc_etapa), x2, y_row, max_w, size=10, leading=13)
        y_row -= 0.18 * cm

    y -= (cron_h + 0.65 * cm)

    # =========================
    # Seção: Condições (menos genérico, mais “agência”)
    # =========================
    validity_days = data.get("validity_days")
    try:
        validity_days = int(validity_days) if validity_days is not None else 7
    except Exception:
        validity_days = 7

    payment_terms = data.get("payment_terms")
    if not isinstance(payment_terms, list) or not payment_terms:
        payment_terms = [
            f"Validade desta proposta: {validity_days} dias.",
            "Pagamento: definir no aceite (à vista / parcelado / conforme combinado).",
            "Ajustes dentro do escopo: 1 rodada incluída. Extras são orçados.",
            "Itens não descritos no escopo não estão inclusos.",
        ]

    y = _ensure_space(c, y, needed=3.0 * cm, width=width, height=height, brand=brand, tagline=tagline)
    _section_title(c, margin_x, y, "CONDIÇÕES")
    y -= 0.55 * cm

    cond_h = 2.9 * cm
    _card(c, margin_x, y, content_w, cond_h, fill=(0.97, 0.98, 1.0))
    y_cond = y - 0.75 * cm
    y_cond = _bullets(c, payment_terms[:6], margin_x + 0.65 * cm, y_cond, content_w - 1.3 * cm)
    y -= (cond_h + 0.65 * cm)

    # =========================
    # Seção: Próximos passos (CTA)
    # =========================
    accept_url = _safe(data.get("accept_url"))

    y = _ensure_space(c, y, needed=2.6 * cm, width=width, height=height, brand=brand, tagline=tagline)
    _section_title(c, margin_x, y, "PRÓXIMOS PASSOS")
    y -= 0.55 * cm

    steps_h = 2.45 * cm
    _card(c, margin_x, y, content_w, steps_h, fill=(0.06, 0.10, 0.18))
    c.setFillColor(colors.white)
    c.setFont("Helvetica", 10)
    c.drawString(margin_x + 0.65 * cm, y - 0.85 * cm, "1) Aprovar a proposta")
    c.drawString(margin_x + 0.65 * cm, y - 1.35 * cm, "2) Confirmar pagamento / condições")
    c.drawString(margin_x + 0.65 * cm, y - 1.85 * cm, "3) Início do projeto e execução")

    if accept_url:
        c.setFont("Helvetica", 9)
        c.setFillColorRGB(0.86, 0.90, 1.00)
        c.drawString(margin_x + 0.65 * cm, y - 2.20 * cm, f"Aceite online: {accept_url}")

    y -= (steps_h + 0.7 * cm)

    # =========================
    # Assinatura (fecha melhor)
    # =========================
    y = _ensure_space(c, y, needed=2.1 * cm, width=width, height=height, brand=brand, tagline=tagline)

    c.setStrokeColorRGB(0.75, 0.80, 0.90)
    c.setLineWidth(1)
    c.line(margin_x, y, margin_x + 9.5 * cm, y)

    c.setFont("Helvetica", 10)
    c.setFillColorRGB(0.12, 0.16, 0.26)
    c.drawString(margin_x, y - 0.45 * cm, "Assinatura / Responsável")

    c.setFont("Helvetica", 9)
    c.setFillColorRGB(0.45, 0.50, 0.62)
    c.drawString(margin_x, y - 0.95 * cm, f"Local/Data: ____/____/______")

    # Rodapé (só FREE)
    if footer_text:
        c.setFont("Helvetica", 8)
        c.setFillColorRGB(0.45, 0.50, 0.62)
        c.drawString(margin_x, 1.5 * cm, footer_text)

    c.showPage()
    c.save()

    buffer.seek(0)
    return buffer.getvalue()