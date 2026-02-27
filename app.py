from fastapi import FastAPI, Request, Form, Depends, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from passlib.hash import pbkdf2_sha256
from datetime import datetime, timedelta, date
import os
import requests
import subprocess
import sys
import secrets
import hashlib
from urllib.parse import quote_plus
import re
import json

from db import SessionLocal, engine, Base
from models import (
    User, Proposal, UserSession,
    ProposalItem, ProposalVersion,
    PaymentStage, PaymentReminder,
    FollowUpSchedule
)
from pdf_gen import generate_proposal_pdf


# ====== migração leve ======
try:
    subprocess.run([sys.executable, "migrate.py"], check=False)
except Exception:
    pass

Base.metadata.create_all(bind=engine)

app = FastAPI()
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


SESSION_COOKIE = "session_token"

APP_BASE_URL = os.getenv("APP_BASE_URL", "").strip().rstrip("/")
COOKIE_SECURE = True if (APP_BASE_URL.startswith("https://")) else False

ASAAS_API_KEY = os.getenv("ASAAS_API_KEY", "").strip()
ASAAS_ENV = os.getenv("ASAAS_ENV", "sandbox").strip().lower()
ASAAS_WEBHOOK_TOKEN = os.getenv("ASAAS_WEBHOOK_TOKEN", "").strip()


def asaas_api_base() -> str:
    return "https://api.asaas.com/v3" if ASAAS_ENV == "prod" else "https://api-sandbox.asaas.com/v3"


def asaas_headers():
    return {
        "access_token": ASAAS_API_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.utcnow()


def get_current_user(request: Request, db: Session) -> User | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    token_hash = _sha256_hex(token)
    sess = db.query(UserSession).filter(UserSession.token_hash == token_hash).first()
    if not sess:
        return None
    if sess.expires_at < _now():
        db.delete(sess)
        db.commit()
        return None
    return db.query(User).filter(User.id == sess.user_id).first()


def create_session(user: User) -> tuple[str, UserSession]:
    token = secrets.token_urlsafe(32)
    token_hash = _sha256_hex(token)
    expires = _now() + timedelta(days=30)
    sess = UserSession(user_id=user.id, token_hash=token_hash, expires_at=expires)
    return token, sess


def is_pro_active(user: User) -> bool:
    if user.plan == "pro":
        if user.paid_until and user.paid_until >= _now():
            return True
        if not user.paid_until:
            return True
        return False
    if user.paid_until and user.paid_until >= _now():
        return True
    return False


def set_user_pro_month(db: Session, user: User, paid_until: datetime, subscription_id: str | None = None, customer_id: str | None = None):
    user.plan = "pro"
    user.proposal_limit = 999999
    user.delete_credits = 999999
    user.plan_updated_at = _now()
    user.paid_until = paid_until
    if subscription_id:
        user.asaas_subscription_id = subscription_id
    if customer_id:
        user.asaas_customer_id = customer_id
    db.add(user)
    db.commit()


def base_url_from_request(request: Request) -> str:
    if APP_BASE_URL:
        return APP_BASE_URL
    return str(request.base_url).rstrip("/")


def normalize_phone_br(phone: str) -> str | None:
    if not phone:
        return None
    digits = re.sub(r"\D", "", phone).lstrip("0")
    if not digits:
        return None
    if digits.startswith("55"):
        core = digits[2:]
    else:
        core = digits
    if len(core) not in (10, 11):
        return None
    return "55" + core


def whatsapp_url(phone_digits_55: str, text: str) -> str:
    return f"https://wa.me/{phone_digits_55}?text={quote_plus(text)}"


def proposal_public_link(request: Request, p: Proposal) -> str:
    return f"{base_url_from_request(request)}/p/{p.public_id}"


def status_label(s: str) -> str:
    return {
        "created": "Criado",
        "sent": "Enviado",
        "viewed": "Visualizado",
        "accepted": "Aceito",
    }.get(s or "", s or "Criado")


def brl_to_cents(v: str) -> int:
    if not v:
        return 0
    s = str(v).strip()
    s = re.sub(r"[^\d,\.]", "", s)
    if not s:
        return 0
    tmp = s.replace(".", "").replace(",", ".")
    try:
        n = float(tmp)
        return int(round(n * 100))
    except Exception:
        return 0


def cents_to_brl(cents: int) -> str:
    n = max(0, int(cents)) / 100.0
    return f"R$ {n:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def compute_total(items: list[ProposalItem], overhead_percent: int, margin_percent: int) -> int:
    base = sum(int(it.line_total_cents or 0) for it in items)
    overhead_percent = max(0, int(overhead_percent or 0))
    margin_percent = max(0, int(margin_percent or 0))
    total = base
    total += int(round(base * (overhead_percent / 100.0)))
    total += int(round(base * (margin_percent / 100.0)))
    return max(0, total)


def rebuild_items_from_form(descs: list[str], qtys: list[str], units: list[str], unit_prices: list[str]) -> list[ProposalItem]:
    items: list[ProposalItem] = []
    for i in range(min(len(descs), len(qtys), len(units), len(unit_prices))):
        d = (descs[i] or "").strip()
        if not d:
            continue
        try:
            q = float(str(qtys[i] or "1").replace(",", "."))
            if q <= 0:
                q = 1.0
        except Exception:
            q = 1.0
        u = (units[i] or "").strip() or ""
        up_cents = brl_to_cents(unit_prices[i] or "0")
        line = int(round(q * up_cents))
        items.append(ProposalItem(sort=i, description=d, qty=q, unit=u, unit_price_cents=up_cents, line_total_cents=line))
    return items


def upsert_payment_stages(db: Session, p: Proposal, p1: int, p2: int, p3: int):
    # normaliza percentuais
    p1 = max(0, min(100, int(p1 or 0)))
    p2 = max(0, min(100, int(p2 or 0)))
    p3 = max(0, min(100, int(p3 or 0)))

    total_percent = p1 + p2 + p3
    if total_percent == 0:
        p1, p2, p3 = 30, 40, 30
    elif total_percent != 100:
        # ajusta proporcionalmente, simples
        factor = 100 / total_percent
        p1 = int(round(p1 * factor))
        p2 = int(round(p2 * factor))
        p3 = 100 - p1 - p2

    total = int(p.total_cents or 0)

    def amt(percent: int) -> int:
        return int(round(total * (percent / 100.0)))

    existing = db.query(PaymentStage).filter(PaymentStage.proposal_id == p.id).order_by(PaymentStage.id.asc()).all()
    # remove e recria (MVP simples)
    for e in existing:
        db.delete(e)
    db.commit()

    stages = [
        PaymentStage(proposal_id=p.id, title="Sinal", percent=p1, amount_cents=amt(p1), due_at=_now(), status="pending"),
        PaymentStage(proposal_id=p.id, title="Etapa 1", percent=p2, amount_cents=amt(p2), due_at=None, status="pending"),
        PaymentStage(proposal_id=p.id, title="Etapa 2", percent=p3, amount_cents=amt(p3), due_at=None, status="pending"),
    ]
    for st in stages:
        db.add(st)
    db.commit()


def ensure_signal_reminders(db: Session, p: Proposal):
    # cria lembretes só para o sinal (stage[0])
    signal = (
        db.query(PaymentStage)
        .filter(PaymentStage.proposal_id == p.id, PaymentStage.title == "Sinal")
        .first()
    )
    if not signal or signal.status == "paid":
        return

    existing = db.query(PaymentReminder).filter(PaymentReminder.stage_id == signal.id).all()
    if existing:
        return

    # D0, D+1, D+3
    for days in (0, 1, 3):
        db.add(PaymentReminder(stage_id=signal.id, due_at=_now() + timedelta(days=days), status="pending"))
    db.commit()


def ensure_followups(db: Session, p: Proposal):
    existing = db.query(FollowUpSchedule).filter(FollowUpSchedule.proposal_id == p.id).all()
    existing_steps = {f.step for f in existing}
    anchor = p.last_activity_at or p.created_at or _now()
    for step in (1, 3, 7):
        if step in existing_steps:
            continue
        db.add(FollowUpSchedule(proposal_id=p.id, step=step, due_at=anchor + timedelta(days=step), status="pending"))
    db.commit()


def build_send_message(owner: User, p: Proposal, link: str) -> str:
    validity = f" (válido até {p.valid_until.strftime('%d/%m')})" if p.valid_until else ""
    return (
        f"Fala {p.client_name}! Aqui é {owner.display_name or (owner.company_name or 'a gente')}.\n"
        f"Te enviei o *orçamento* do *{p.project_name}*{validity}.\n\n"
        f"👉 Link: {link}\n\n"
        f"Se fizer sentido, você consegue *aprovar por lá* em 10 segundos. "
        f"Se quiser ajustar algo, me fala que eu atualizo rapidinho."
    )


def build_followup_message(owner: User, p: Proposal, link: str, step: int) -> str:
    if step == 1:
        return f"Oi {p.client_name}! Conseguiu ver o orçamento do *{p.project_name}*?\nLink: {link}\n\nSe quiser, ajusto rapidinho."
    if step == 3:
        return f"{p.client_name}, só pra eu organizar minha agenda:\nVocê quer seguir com o *{p.project_name}* essa semana?\n\nLink: {link}"
    return f"Oi {p.client_name}! Último toque pra eu não te incomodar:\nVou encerrar esse orçamento e liberar agenda.\nSe ainda tiver interesse, me chama que eu reabro.\n\nLink: {link}"


def build_pix_message(owner: User, p: Proposal, amount_cents: int) -> str:
    pix_line = f"Chave Pix: {owner.pix_key}" if owner.pix_key else "(Pix não configurado)"
    recebedor = f"Recebedor: {owner.pix_name}\n" if owner.pix_name else ""
    return (
        f"{p.client_name}, pra reservar a data do *{p.project_name}* o sinal é *{cents_to_brl(amount_cents)}*.\n\n"
        f"{recebedor}{pix_line}\n\n"
        f"Assim que pagar, me manda o comprovante aqui e eu confirmo ✅"
    )


# ====== HOME ======
@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return RedirectResponse("/dashboard", status_code=302)


# ====== AUTH ======
@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    return templates.TemplateResponse("register.html", {"request": request, "error": None})


@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == email.strip().lower()).first()
    if (not user) or (not pbkdf2_sha256.verify(password, user.password_hash)):
        return templates.TemplateResponse("login.html", {"request": request, "error": "Email ou senha inválidos."})

    token, sess = create_session(user)
    db.add(sess)
    db.commit()

    resp = RedirectResponse("/dashboard", status_code=302)
    resp.set_cookie(SESSION_COOKIE, token, httponly=True, secure=COOKIE_SECURE, samesite="lax", max_age=60 * 60 * 24 * 30)
    return resp


@app.post("/register")
def register(request: Request, email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    email = email.strip().lower()
    if db.query(User).filter(User.email == email).first():
        return templates.TemplateResponse("register.html", {"request": request, "error": "Esse email já existe. Faça login."})

    user = User(email=email, password_hash=pbkdf2_sha256.hash(password), proposal_limit=5, plan="free", delete_credits=1)
    db.add(user)
    db.commit()
    db.refresh(user)

    token, sess = create_session(user)
    db.add(sess)
    db.commit()

    resp = RedirectResponse("/dashboard", status_code=302)
    resp.set_cookie(SESSION_COOKIE, token, httponly=True, secure=COOKIE_SECURE, samesite="lax", max_age=60 * 60 * 24 * 30)
    return resp


@app.get("/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        th = _sha256_hex(token)
        sess = db.query(UserSession).filter(UserSession.token_hash == th).first()
        if sess:
            db.delete(sess)
            db.commit()
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


# ====== DASHBOARD ======
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, status: str = "all", db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    q = db.query(Proposal).filter(Proposal.owner_id == user.id)
    if status == "accepted":
        q = q.filter(Proposal.accepted_at.isnot(None))
    elif status == "pending":
        q = q.filter(Proposal.accepted_at.is_(None))

    proposals = q.order_by(Proposal.created_at.desc()).all()

    total = db.query(Proposal).filter(Proposal.owner_id == user.id).count()
    accepted = db.query(Proposal).filter(Proposal.owner_id == user.id, Proposal.accepted_at.isnot(None)).count()
    rate = round((accepted / total) * 100) if total else 0

    now = _now()

    due_followups = (
        db.query(FollowUpSchedule)
        .join(Proposal, Proposal.id == FollowUpSchedule.proposal_id)
        .filter(Proposal.owner_id == user.id, Proposal.accepted_at.is_(None),
                FollowUpSchedule.status == "pending", FollowUpSchedule.due_at <= now)
        .order_by(FollowUpSchedule.due_at.asc())
        .all()
    )

    due_payment_reminders = (
        db.query(PaymentReminder)
        .join(PaymentStage, PaymentStage.id == PaymentReminder.stage_id)
        .join(Proposal, Proposal.id == PaymentStage.proposal_id)
        .filter(Proposal.owner_id == user.id,
                Proposal.accepted_at.isnot(None),
                PaymentStage.status == "pending",
                PaymentReminder.status == "pending",
                PaymentReminder.due_at <= now)
        .order_by(PaymentReminder.due_at.asc())
        .all()
    )

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "user": user,
        "owner": user,
        "proposals": proposals,
        "total": total,
        "accepted": accepted,
        "rate": rate,
        "status": status,
        "due_followups": due_followups,
        "due_payment_reminders": due_payment_reminders,
        "status_label": status_label,
    })


# ====== NEW PROPOSAL ======
@app.get("/proposals/new", response_class=HTMLResponse)
def new_proposal_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse("new_proposal.html", {"request": request, "error": None, "user": user})


@app.post("/proposals/new")
def create_proposal(
    request: Request,
    client_name: str = Form(...),
    client_whatsapp: str = Form(""),
    project_name: str = Form(...),
    description: str = Form(...),
    price: str = Form(""),
    deadline: str = Form(...),
    validity_days: int = Form(7),
    overhead_percent: int = Form(10),
    margin_percent: int = Form(0),
    p1_percent: int = Form(30),
    p2_percent: int = Form(40),
    p3_percent: int = Form(30),
    payment_note: str = Form(""),
    item_desc: list[str] = Form([]),
    item_qty: list[str] = Form([]),
    item_unit: list[str] = Form([]),
    item_unit_price: list[str] = Form([]),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    if not is_pro_active(user):
        count = db.query(Proposal).filter(Proposal.owner_id == user.id).count()
        if count >= (user.proposal_limit or 5):
            return RedirectResponse("/pricing", status_code=302)

    valid_until = _now() + timedelta(days=max(1, min(int(validity_days or 7), 30)))

    p = Proposal(
        client_name=client_name.strip(),
        client_whatsapp=client_whatsapp.strip() or None,
        project_name=project_name.strip(),
        description=description.strip(),
        price=(price or "").strip(),
        deadline=deadline.strip(),
        owner_id=user.id,
        status="created",
        valid_until=valid_until,
        last_activity_at=_now(),
        overhead_percent=int(overhead_percent or 0),
        margin_percent=int(margin_percent or 0),
        revision=1,
        updated_at=_now(),
    )
    db.add(p)
    db.commit()
    db.refresh(p)

    items = rebuild_items_from_form(item_desc, item_qty, item_unit, item_unit_price)
    for it in items:
        it.proposal_id = p.id
        db.add(it)
    db.commit()

    # total calculado
    db.refresh(p)
    db.refresh(p)  # garante id
    saved_items = db.query(ProposalItem).filter(ProposalItem.proposal_id == p.id).order_by(ProposalItem.sort.asc()).all()
    total_cents = compute_total(saved_items, p.overhead_percent, p.margin_percent)

    # se usuário digitou preço, usa como override
    if p.price:
        override = brl_to_cents(p.price)
        if override > 0:
            total_cents = override

    p.total_cents = total_cents
    db.add(p)
    db.commit()

    upsert_payment_stages(db, p, p1_percent, p2_percent, p3_percent)

    return RedirectResponse("/dashboard", status_code=302)


# ====== EDIT PROPOSAL (VERSÃO) ======
@app.get("/proposals/{proposal_id}/edit", response_class=HTMLResponse)
def edit_proposal_page(proposal_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    p = db.query(Proposal).filter(Proposal.id == proposal_id, Proposal.owner_id == user.id).first()
    if not p:
        return RedirectResponse("/dashboard", status_code=302)

    # percent atuais (se não existir, padrão)
    stages = db.query(PaymentStage).filter(PaymentStage.proposal_id == p.id).order_by(PaymentStage.id.asc()).all()
    p1 = stages[0].percent if len(stages) > 0 else 30
    p2 = stages[1].percent if len(stages) > 1 else 40
    p3 = stages[2].percent if len(stages) > 2 else 30

    return templates.TemplateResponse("edit_proposal.html", {
        "request": request,
        "p": p,
        "p1": p1, "p2": p2, "p3": p3,
        "payment_note": "",
        "error": None
    })


@app.post("/proposals/{proposal_id}/edit")
def edit_proposal_save(
    proposal_id: int,
    request: Request,
    client_name: str = Form(...),
    client_whatsapp: str = Form(""),
    project_name: str = Form(...),
    description: str = Form(...),
    deadline: str = Form(...),
    overhead_percent: int = Form(10),
    margin_percent: int = Form(0),
    price: str = Form(""),
    p1_percent: int = Form(30),
    p2_percent: int = Form(40),
    p3_percent: int = Form(30),
    payment_note: str = Form(""),
    item_desc: list[str] = Form([]),
    item_qty: list[str] = Form([]),
    item_unit: list[str] = Form([]),
    item_unit_price: list[str] = Form([]),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    p = db.query(Proposal).filter(Proposal.id == proposal_id, Proposal.owner_id == user.id).first()
    if not p:
        return RedirectResponse("/dashboard", status_code=302)

    # salva snapshot antes (histórico)
    snapshot = {
        "client_name": p.client_name,
        "client_whatsapp": p.client_whatsapp,
        "project_name": p.project_name,
        "description": p.description,
        "deadline": p.deadline,
        "overhead_percent": p.overhead_percent,
        "margin_percent": p.margin_percent,
        "price": p.price,
        "total_cents": p.total_cents,
        "items": [
            {"description": it.description, "qty": it.qty, "unit": it.unit, "unit_price_cents": it.unit_price_cents, "line_total_cents": it.line_total_cents}
            for it in p.items
        ],
        "payment_stages": [
            {"title": st.title, "percent": st.percent, "amount_cents": st.amount_cents, "status": st.status}
            for st in p.payment_stages
        ]
    }
    db.add(ProposalVersion(proposal_id=p.id, revision=p.revision, snapshot_json=json.dumps(snapshot, ensure_ascii=False)))
    db.commit()

    # incrementa revision
    p.revision = int(p.revision or 1) + 1
    p.updated_at = _now()

    p.client_name = client_name.strip()
    p.client_whatsapp = client_whatsapp.strip() or None
    p.project_name = project_name.strip()
    p.description = description.strip()
    p.deadline = deadline.strip()
    p.overhead_percent = int(overhead_percent or 0)
    p.margin_percent = int(margin_percent or 0)
    p.price = (price or "").strip()

    # recria itens
    for it in list(p.items):
        db.delete(it)
    db.commit()

    items = rebuild_items_from_form(item_desc, item_qty, item_unit, item_unit_price)
    for it in items:
        it.proposal_id = p.id
        db.add(it)
    db.commit()

    saved_items = db.query(ProposalItem).filter(ProposalItem.proposal_id == p.id).order_by(ProposalItem.sort.asc()).all()
    total_cents = compute_total(saved_items, p.overhead_percent, p.margin_percent)
    if p.price:
        override = brl_to_cents(p.price)
        if override > 0:
            total_cents = override

    p.total_cents = total_cents
    db.add(p)
    db.commit()

    upsert_payment_stages(db, p, p1_percent, p2_percent, p3_percent)

    # se já foi enviado/visualizado, mantém pipeline, mas atualiza atividade
    p.last_activity_at = _now()
    db.add(p)
    db.commit()

    return RedirectResponse("/dashboard", status_code=302)


# ====== SEND / FOLLOWUP ======
@app.get("/proposals/{proposal_id}/send_whatsapp")
def send_whatsapp(proposal_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    p = db.query(Proposal).filter(Proposal.id == proposal_id, Proposal.owner_id == user.id).first()
    if not p:
        return RedirectResponse("/dashboard", status_code=302)

    phone = normalize_phone_br(p.client_whatsapp or "")
    if not phone:
        return RedirectResponse(f"/proposals/{p.id}/edit", status_code=302)

    link = proposal_public_link(request, p)
    text = build_send_message(user, p, link)

    if p.status != "accepted":
        p.status = "sent"
    p.last_activity_at = _now()
    db.add(p)
    db.commit()

    ensure_followups(db, p)

    return RedirectResponse(whatsapp_url(phone, text), status_code=302)


@app.get("/proposals/{proposal_id}/followup/{step}")
def send_followup(proposal_id: int, step: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    p = db.query(Proposal).filter(Proposal.id == proposal_id, Proposal.owner_id == user.id).first()
    if not p or p.accepted_at is not None:
        return RedirectResponse("/dashboard", status_code=302)

    phone = normalize_phone_br(p.client_whatsapp or "")
    if not phone:
        return RedirectResponse(f"/proposals/{p.id}/edit", status_code=302)

    ensure_followups(db, p)
    f = db.query(FollowUpSchedule).filter(FollowUpSchedule.proposal_id == p.id, FollowUpSchedule.step == step, FollowUpSchedule.status == "pending").first()

    link = proposal_public_link(request, p)
    text = build_followup_message(user, p, link, step)

    if f:
        f.status = "sent"
        f.sent_at = _now()
        db.add(f)
        db.commit()

    return RedirectResponse(whatsapp_url(phone, text), status_code=302)


# ====== PUBLIC (TRACK + ACCEPT) ======
@app.get("/p/{public_id}", response_class=HTMLResponse)
def public_proposal(public_id: str, request: Request, db: Session = Depends(get_db)):
    p = db.query(Proposal).filter(Proposal.public_id == public_id).first()
    if not p:
        return HTMLResponse("Orçamento não encontrado.", status_code=404)

    owner = db.query(User).filter(User.id == p.owner_id).first()
    base_url = base_url_from_request(request)

    view_cookie = f"pv_{public_id}"
    last_seen = request.cookies.get(view_cookie)

    should_count = True
    if last_seen:
        try:
            last_dt = datetime.fromisoformat(last_seen)
            if (_now() - last_dt) < timedelta(minutes=10):
                should_count = False
        except Exception:
            should_count = True

    if should_count:
        p.view_count = (p.view_count or 0) + 1
        if not p.first_viewed_at:
            p.first_viewed_at = _now()
        p.last_viewed_at = _now()
        p.last_activity_at = _now()
        if p.status in ("sent", "created") and p.accepted_at is None:
            p.status = "viewed"
        db.add(p)
        db.commit()

    resp = templates.TemplateResponse("proposal_public.html", {
        "request": request,
        "p": p,
        "owner": owner,
        "base_url": base_url,
        "status_label": status_label,
    })
    resp.set_cookie(view_cookie, _now().isoformat(), max_age=60 * 60 * 24 * 30, samesite="lax")
    return resp


@app.post("/p/{public_id}/accept", response_class=HTMLResponse)
def accept_proposal(public_id: str, request: Request, name: str = Form(...), email: str = Form(""), db: Session = Depends(get_db)):
    p = db.query(Proposal).filter(Proposal.public_id == public_id).first()
    if not p:
        return HTMLResponse("Orçamento não encontrado.", status_code=404)

    owner = db.query(User).filter(User.id == p.owner_id).first()
    base_url = base_url_from_request(request)

    if p.accepted_at is None:
        p.accepted_at = _now()
        p.accepted_name = name.strip()
        p.accepted_email = (email or "").strip() or None
        p.status = "accepted"
        p.last_activity_at = _now()
        db.add(p)
        db.commit()
        db.refresh(p)

        # ao aceitar: cria lembretes do sinal
        ensure_signal_reminders(db, p)

    return templates.TemplateResponse("accepted.html", {"request": request, "p": p, "owner": owner, "base_url": base_url})


# ====== PDF ======
@app.get("/p/{public_id}/pdf")
def public_pdf(public_id: str, request: Request, db: Session = Depends(get_db)):
    p = db.query(Proposal).filter(Proposal.public_id == public_id).first()
    if not p:
        return HTMLResponse("Orçamento não encontrado.", status_code=404)

    owner = db.query(User).filter(User.id == p.owner_id).first()
    accept_url = f"{base_url_from_request(request)}/p/{p.public_id}"

    items = [
        {"description": it.description, "qty": it.qty, "unit": it.unit, "unit_price_cents": it.unit_price_cents, "line_total_cents": it.line_total_cents}
        for it in p.items
    ]
    stages = [
        {"title": st.title, "percent": st.percent, "amount_cents": st.amount_cents}
        for st in p.payment_stages
    ]

    pdf_bytes = generate_proposal_pdf({
        "client_name": p.client_name,
        "project_name": p.project_name,
        "description": p.description,
        "price": p.price,
        "deadline": p.deadline,
        "author_email": owner.email if owner else "",
        "author_name": owner.display_name if owner and owner.display_name else "",
        "company_name": owner.company_name if owner and owner.company_name else "",
        "phone": owner.phone if owner and owner.phone else "",
        "is_pro": (owner is not None and is_pro_active(owner)),
        "items": items,
        "total_cents": p.total_cents,
        "payment_stages": stages,
        "accept_url": accept_url,
    })

    filename = f"orcamento_{p.client_name.replace(' ', '')}_{p.public_id}.pdf"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return Response(content=pdf_bytes, media_type="application/pdf", headers=headers)


@app.get("/proposals/{proposal_id}/pdf")
def download_pdf(proposal_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    p = db.query(Proposal).filter(Proposal.id == proposal_id, Proposal.owner_id == user.id).first()
    if not p:
        return RedirectResponse("/dashboard", status_code=302)

    accept_url = f"{base_url_from_request(request)}/p/{p.public_id}"

    items = [
        {"description": it.description, "qty": it.qty, "unit": it.unit, "unit_price_cents": it.unit_price_cents, "line_total_cents": it.line_total_cents}
        for it in p.items
    ]
    stages = [
        {"title": st.title, "percent": st.percent, "amount_cents": st.amount_cents}
        for st in p.payment_stages
    ]

    pdf_bytes = generate_proposal_pdf({
        "client_name": p.client_name,
        "project_name": p.project_name,
        "description": p.description,
        "price": p.price,
        "deadline": p.deadline,
        "author_email": user.email,
        "author_name": user.display_name or "",
        "company_name": user.company_name or "",
        "phone": user.phone or "",
        "is_pro": is_pro_active(user),
        "items": items,
        "total_cents": p.total_cents,
        "payment_stages": stages,
        "accept_url": accept_url,
    })

    filename = f"orcamento_{p.client_name.replace(' ', '')}_{p.id}.pdf"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return Response(content=pdf_bytes, media_type="application/pdf", headers=headers)


# ====== PAYMENTS: REMINDERS + MARK PAID ======
@app.get("/payments/reminders/{reminder_id}/send_whatsapp")
def send_payment_reminder(reminder_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    r = db.query(PaymentReminder).filter(PaymentReminder.id == reminder_id).first()
    if not r:
        return RedirectResponse("/dashboard", status_code=302)

    st = db.query(PaymentStage).filter(PaymentStage.id == r.stage_id).first()
    if not st:
        return RedirectResponse("/dashboard", status_code=302)

    p = db.query(Proposal).filter(Proposal.id == st.proposal_id, Proposal.owner_id == user.id).first()
    if not p:
        return RedirectResponse("/dashboard", status_code=302)

    phone = normalize_phone_br(p.client_whatsapp or "")
    if not phone:
        return RedirectResponse(f"/proposals/{p.id}/edit", status_code=302)

    text = build_pix_message(user, p, st.amount_cents)

    r.status = "sent"
    r.sent_at = _now()
    db.add(r)
    db.commit()

    return RedirectResponse(whatsapp_url(phone, text), status_code=302)


@app.post("/payments/stages/{stage_id}/mark_paid")
def mark_stage_paid(stage_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    st = db.query(PaymentStage).filter(PaymentStage.id == stage_id).first()
    if not st:
        return RedirectResponse("/dashboard", status_code=302)

    p = db.query(Proposal).filter(Proposal.id == st.proposal_id, Proposal.owner_id == user.id).first()
    if not p:
        return RedirectResponse("/dashboard", status_code=302)

    st.status = "paid"
    st.paid_at = _now()
    db.add(st)

    # marca lembretes como skipped
    for r in st.reminders:
        if r.status == "pending":
            r.status = "skipped"
            db.add(r)

    db.commit()
    return RedirectResponse("/dashboard", status_code=302)


# ====== PROFILE / BILLING / STATIC ======
@app.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse("profile.html", {"request": request, "user": user, "saved": False, "error": None})


@app.post("/profile")
def profile_save(
    request: Request,
    display_name: str = Form(""),
    company_name: str = Form(""),
    phone: str = Form(""),
    cpf_cnpj: str = Form(""),
    pix_key: str = Form(""),
    pix_name: str = Form(""),
    pix_city: str = Form(""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    user.display_name = display_name.strip() or None
    user.company_name = company_name.strip() or None
    user.phone = phone.strip() or None
    user.cpf_cnpj = cpf_cnpj.strip() or None
    user.pix_key = pix_key.strip() or None
    user.pix_name = pix_name.strip() or None
    user.pix_city = pix_city.strip() or None

    db.add(user)
    db.commit()

    return templates.TemplateResponse("profile.html", {"request": request, "user": user, "saved": True, "error": None})


@app.get("/billing", response_class=HTMLResponse)
def billing(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse("billing.html", {"request": request, "user": user})


@app.get("/pricing", response_class=HTMLResponse)
def pricing(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    return templates.TemplateResponse("pricing.html", {"request": request, "user": user})


@app.get("/terms", response_class=HTMLResponse)
def terms(request: Request):
    return templates.TemplateResponse("terms.html", {"request": request})


@app.get("/privacy", response_class=HTMLResponse)
def privacy(request: Request):
    return templates.TemplateResponse("privacy.html", {"request": request})


@app.get("/support", response_class=HTMLResponse)
def support(request: Request):
    return templates.TemplateResponse("support.html", {"request": request})


# ====== ASAAS (upgrade + webhook) ======
def ensure_asaas_customer(db: Session, user: User) -> str:
    if user.asaas_customer_id:
        return user.asaas_customer_id
    if not ASAAS_API_KEY:
        raise RuntimeError("ASAAS_API_KEY não configurado no Render.")
    name = user.display_name or user.company_name or user.email.split("@")[0]
    payload = {"name": name, "email": user.email}
    if user.cpf_cnpj:
        payload["cpfCnpj"] = user.cpf_cnpj
    r = requests.post(f"{asaas_api_base()}/customers", headers=asaas_headers(), json=payload, timeout=30)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Erro Asaas ao criar customer: {r.status_code} - {r.text}")
    data = r.json()
    cid = data.get("id")
    if not cid:
        raise RuntimeError(f"Asaas não retornou customer id: {data}")
    user.asaas_customer_id = cid
    db.add(user)
    db.commit()
    return cid


@app.get("/upgrade/pro")
def upgrade_pro(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not ASAAS_API_KEY:
        return HTMLResponse("ASAAS_API_KEY não configurado no Render.", status_code=500)
    if is_pro_active(user):
        return RedirectResponse("/billing", status_code=302)
    if not getattr(user, "cpf_cnpj", None):
        return templates.TemplateResponse("profile.html", {"request": request, "user": user, "saved": False,
            "error": "Para assinar o PRO, preencha seu CPF/CNPJ no perfil e salve."})

    customer_id = ensure_asaas_customer(db, user)
    next_due = date.today().strftime("%Y-%m-%d")
    payload = {
        "customer": customer_id,
        "billingType": "CREDIT_CARD",
        "value": 19.90,
        "nextDueDate": next_due,
        "cycle": "MONTHLY",
        "description": "PropoFlow Pro (assinatura mensal)",
        "externalReference": f"user_{user.id}",
    }
    r = requests.post(f"{asaas_api_base()}/subscriptions", headers=asaas_headers(), json=payload, timeout=30)
    if r.status_code not in (200, 201):
        return HTMLResponse(f"Erro Asaas ao criar assinatura: {r.status_code}<br><pre>{r.text}</pre>", status_code=500)

    sub = r.json()
    sub_id = sub.get("id")
    user.asaas_subscription_id = sub_id
    db.add(user)
    db.commit()

    rp = requests.get(f"{asaas_api_base()}/subscriptions/{sub_id}/payments", headers=asaas_headers(), timeout=30)
    payments = rp.json()
    data_list = payments.get("data") if isinstance(payments, dict) else None
    first = data_list[0] if data_list else {}
    invoice_url = first.get("invoiceUrl")
    if not invoice_url:
        return HTMLResponse("Assinatura criada, mas não achei invoiceUrl.", status_code=500)
    return RedirectResponse(invoice_url, status_code=302)


@app.post("/webhooks/asaas")
async def webhooks_asaas(request: Request, db: Session = Depends(get_db)):
    if ASAAS_WEBHOOK_TOKEN:
        token = request.headers.get("asaas-access-token") or request.headers.get("Asaas-Access-Token")
        if token != ASAAS_WEBHOOK_TOKEN:
            return HTMLResponse("unauthorized", status_code=401)

    try:
        body = await request.json()
    except Exception:
        body = {}

    event = (body.get("event") or "").upper()
    payment = body.get("payment") or {}
    subscription = body.get("subscription") or {}

    external_ref = ""
    if isinstance(payment, dict):
        external_ref = payment.get("externalReference") or ""
    if not external_ref and isinstance(subscription, dict):
        external_ref = subscription.get("externalReference") or ""

    if not external_ref.startswith("user_"):
        return {"ok": True}

    try:
        user_id = int(external_ref.replace("user_", ""))
    except Exception:
        return {"ok": True}

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return {"ok": True}

    if event in ("PAYMENT_CONFIRMED", "PAYMENT_RECEIVED", "PAYMENT_APPROVED"):
        paid_until = _now() + timedelta(days=32)
        set_user_pro_month(db, user, paid_until, subscription_id=user.asaas_subscription_id, customer_id=user.asaas_customer_id)
        return {"ok": True}

    return {"ok": True}