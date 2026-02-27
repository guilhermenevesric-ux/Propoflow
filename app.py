# app.py
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

from db import SessionLocal, engine, Base
from models import User, Proposal, UserSession, FollowUpSchedule
from pdf_gen import generate_proposal_pdf


# ====== roda migração leve ======
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


# ==========================
# CONFIG
# ==========================
SESSION_COOKIE = "session_token"

APP_BASE_URL = os.getenv("APP_BASE_URL", "").strip().rstrip("/")
COOKIE_SECURE = True if (APP_BASE_URL.startswith("https://")) else False

ASAAS_API_KEY = os.getenv("ASAAS_API_KEY", "").strip()
ASAAS_ENV = os.getenv("ASAAS_ENV", "sandbox").strip().lower()  # sandbox | prod
ASAAS_WEBHOOK_TOKEN = os.getenv("ASAAS_WEBHOOK_TOKEN", "").strip()


def asaas_api_base() -> str:
    return "https://api.asaas.com/v3" if ASAAS_ENV == "prod" else "https://api-sandbox.asaas.com/v3"


def asaas_headers():
    return {
        "access_token": ASAAS_API_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


# ==========================
# AUTH (SESSÃO SEGURA)
# ==========================
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
        # sessão expirada
        db.delete(sess)
        db.commit()
        return None

    user = db.query(User).filter(User.id == sess.user_id).first()
    return user


def create_session_response(user: User) -> RedirectResponse:
    token = secrets.token_urlsafe(32)
    token_hash = _sha256_hex(token)
    expires = _now() + timedelta(days=30)

    # salva no banco
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


def set_user_pro_month(
    db: Session,
    user: User,
    paid_until: datetime,
    subscription_id: str | None = None,
    customer_id: str | None = None
):
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


def set_user_free(db: Session, user: User):
    user.plan = "free"
    user.proposal_limit = 5
    user.delete_credits = user.delete_credits if (user.delete_credits is not None) else 1
    user.plan_updated_at = _now()
    db.add(user)
    db.commit()


# ==========================
# HELPERS
# ==========================
def base_url_from_request(request: Request) -> str:
    if APP_BASE_URL:
        return APP_BASE_URL
    return str(request.base_url).rstrip("/")


def normalize_phone_br(phone: str) -> str | None:
    """
    Aceita: (11) 99999-9999, 11999999999, +55..., 55...
    Retorna apenas dígitos com DDI 55.
    """
    if not phone:
        return None
    digits = re.sub(r"\D", "", phone)
    if not digits:
        return None

    # remove zeros à esquerda
    digits = digits.lstrip("0")

    if digits.startswith("55"):
        core = digits[2:]
    else:
        core = digits

    # core deve ser DDD (2) + número (8 ou 9)
    if len(core) not in (10, 11):
        return None

    return "55" + core


def whatsapp_url(phone_digits_55: str, text: str) -> str:
    return f"https://wa.me/{phone_digits_55}?text={quote_plus(text)}"


def proposal_public_link(request: Request, p: Proposal) -> str:
    return f"{base_url_from_request(request)}/p/{p.public_id}"


def status_label(s: str) -> str:
    return {
        "created": "Criada",
        "sent": "Enviada",
        "viewed": "Visualizada",
        "accepted": "Aceita",
    }.get(s or "", s or "Criada")


def build_send_message(owner: User, p: Proposal, link: str) -> str:
    # mensagem bem brasileira e humana
    validity = ""
    if p.valid_until:
        validity = f" (válido até {p.valid_until.strftime('%d/%m')})"

    brand = owner.company_name or owner.display_name or "a gente"
    return (
        f"Fala {p.client_name}! Aqui é {owner.display_name or brand}.\n"
        f"Te enviei o orçamento do *{p.project_name}*{validity}.\n\n"
        f"👉 Link: {link}\n\n"
        f"Se fizer sentido, você consegue *aprovar por lá mesmo* em 10s. "
        f"Se quiser ajustar algo, me fala que eu atualizo rapidinho."
    )


def build_followup_message(owner: User, p: Proposal, link: str, step: int) -> str:
    if step == 1:
        return (
            f"Oi {p.client_name}! Conseguiu ver o orçamento do *{p.project_name}*?\n"
            f"Link: {link}\n\n"
            f"Se você quiser, eu ajusto algum ponto rapidinho."
        )
    if step == 3:
        return (
            f"{p.client_name}, só pra eu organizar minha agenda:\n"
            f"Você quer seguir com o *{p.project_name}* essa semana ou prefere deixar pra depois?\n\n"
            f"Link: {link}"
        )
    # step 7
    return (
        f"Oi {p.client_name}! Último toque pra eu não te incomodar:\n"
        f"vou encerrar esse orçamento e liberar agenda.\n"
        f"Se ainda tiver interesse, me chama que eu reabro e atualizo valores.\n\n"
        f"Link: {link}"
    )


def ensure_followups(db: Session, p: Proposal):
    """
    Cria D+1, D+3, D+7 se ainda não existirem.
    """
    existing = db.query(FollowUpSchedule).filter(FollowUpSchedule.proposal_id == p.id).all()
    existing_steps = {f.step for f in existing}

    # referência: se enviada, conta a partir de last_activity_at; senão, created_at
    anchor = p.last_activity_at or p.created_at or _now()

    for step in (1, 3, 7):
        if step in existing_steps:
            continue
        due = anchor + timedelta(days=step)
        db.add(FollowUpSchedule(proposal_id=p.id, step=step, due_at=due, status="pending"))

    db.commit()


# ==========================
# HOME
# ==========================
@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return RedirectResponse("/dashboard", status_code=302)


# ==========================
# AUTH PAGES
# ==========================
@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    return templates.TemplateResponse("register.html", {"request": request, "error": None})


@app.post("/login")
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.email == email.strip().lower()).first()
    if (not user) or (not pbkdf2_sha256.verify(password, user.password_hash)):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Email ou senha inválidos."},
        )

    token, sess = create_session_response(user)
    db.add(sess)
    db.commit()

    resp = RedirectResponse("/dashboard", status_code=302)
    resp.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
        max_age=60 * 60 * 24 * 30,
    )
    return resp


@app.post("/register")
def register(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    email = email.strip().lower()
    if db.query(User).filter(User.email == email).first():
        return templates.TemplateResponse(
            "register.html",
            {"request": request, "error": "Esse email já existe. Faça login."},
        )

    user = User(
        email=email,
        password_hash=pbkdf2_sha256.hash(password),
        proposal_limit=5,
        plan="free",
        delete_credits=1,
    )

    db.add(user)
    db.commit()
    db.refresh(user)

    token, sess = create_session_response(user)
    db.add(sess)
    db.commit()

    resp = RedirectResponse("/dashboard", status_code=302)
    resp.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
        max_age=60 * 60 * 24 * 30,
    )
    return resp


@app.get("/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        token_hash = _sha256_hex(token)
        sess = db.query(UserSession).filter(UserSession.token_hash == token_hash).first()
        if sess:
            db.delete(sess)
            db.commit()

    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


# ==========================
# DASHBOARD + FOLLOWUPS
# ==========================
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
    accepted = db.query(Proposal).filter(
        Proposal.owner_id == user.id,
        Proposal.accepted_at.isnot(None)
    ).count()
    rate = round((accepted / total) * 100) if total else 0

    now = _now()
    due_followups = (
        db.query(FollowUpSchedule)
        .join(Proposal, Proposal.id == FollowUpSchedule.proposal_id)
        .filter(
            Proposal.owner_id == user.id,
            Proposal.accepted_at.is_(None),
            FollowUpSchedule.status == "pending",
            FollowUpSchedule.due_at <= now
        )
        .order_by(FollowUpSchedule.due_at.asc())
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
        "status_label": status_label,
        "now": now,
    })


# ==========================
# PROPOSALS (CREATE / DELETE / DUPLICATE)
# ==========================
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
    price: str = Form(...),
    deadline: str = Form(...),
    validity_days: int = Form(7),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    # Limite free
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
        price=price.strip(),
        deadline=deadline.strip(),
        owner_id=user.id,
        status="created",
        valid_until=valid_until,
        last_activity_at=_now(),
    )
    db.add(p)
    db.commit()
    db.refresh(p)

    return RedirectResponse("/dashboard", status_code=302)


@app.post("/proposals/{proposal_id}/delete")
def delete_proposal(proposal_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    p = db.query(Proposal).filter(
        Proposal.id == proposal_id,
        Proposal.owner_id == user.id
    ).first()
    if not p:
        return RedirectResponse("/dashboard", status_code=302)

    if not is_pro_active(user) and user.plan == "free":
        credits = user.delete_credits or 0
        if credits <= 0:
            return RedirectResponse("/pricing", status_code=302)
        user.delete_credits = credits - 1
        db.add(user)

    db.delete(p)
    db.commit()
    return RedirectResponse("/dashboard", status_code=302)


@app.get("/proposals/{proposal_id}/duplicate")
def duplicate_proposal(proposal_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    original = db.query(Proposal).filter(
        Proposal.id == proposal_id,
        Proposal.owner_id == user.id
    ).first()
    if not original:
        return RedirectResponse("/dashboard", status_code=302)

    new_p = Proposal(
        client_name=original.client_name,
        client_whatsapp=original.client_whatsapp,
        project_name=original.project_name,
        description=original.description,
        price=original.price,
        deadline=original.deadline,
        owner_id=user.id,
        status="created",
        valid_until=(_now() + timedelta(days=7)),
        last_activity_at=_now(),
    )

    db.add(new_p)
    db.commit()
    return RedirectResponse("/dashboard", status_code=302)


# ==========================
# OWNER ACTIONS: SEND + FOLLOWUP (WHATSAPP 1-CLICK)
# ==========================
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
        return RedirectResponse(f"/proposals/{p.id}/edit_phone", status_code=302)

    link = proposal_public_link(request, p)
    text = build_send_message(user, p, link)

    # marca como enviada
    p.status = "sent" if p.status != "accepted" else p.status
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
    if not p:
        return RedirectResponse("/dashboard", status_code=302)

    if p.accepted_at is not None:
        return RedirectResponse("/dashboard", status_code=302)

    phone = normalize_phone_br(p.client_whatsapp or "")
    if not phone:
        return RedirectResponse(f"/proposals/{p.id}/edit_phone", status_code=302)

    f = db.query(FollowUpSchedule).filter(
        FollowUpSchedule.proposal_id == p.id,
        FollowUpSchedule.step == step,
        FollowUpSchedule.status == "pending"
    ).first()

    # mesmo se não tiver (ex.: migração antiga), cria
    ensure_followups(db, p)
    if not f:
        f = db.query(FollowUpSchedule).filter(
            FollowUpSchedule.proposal_id == p.id,
            FollowUpSchedule.step == step,
            FollowUpSchedule.status == "pending"
        ).first()

    link = proposal_public_link(request, p)
    text = build_followup_message(user, p, link, step)

    if f:
        f.status = "sent"
        f.sent_at = _now()
        db.add(f)

    db.commit()
    return RedirectResponse(whatsapp_url(phone, text), status_code=302)


@app.get("/proposals/{proposal_id}/edit_phone", response_class=HTMLResponse)
def edit_phone_page(proposal_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    p = db.query(Proposal).filter(Proposal.id == proposal_id, Proposal.owner_id == user.id).first()
    if not p:
        return RedirectResponse("/dashboard", status_code=302)

    return HTMLResponse(
        f"""
        <html><head><meta name="viewport" content="width=device-width, initial-scale=1"></head>
        <body style="font-family:Arial;padding:20px;">
          <h3>Falta o WhatsApp do cliente</h3>
          <p>Para enviar e fazer follow-up automático, precisamos do WhatsApp do cliente.</p>
          <form method="post" action="/proposals/{p.id}/edit_phone">
            <input name="client_whatsapp" placeholder="Ex: (11) 99999-9999" style="padding:10px;width:280px;">
            <button style="padding:10px 14px;">Salvar</button>
          </form>
          <p><a href="/dashboard">Voltar</a></p>
        </body></html>
        """,
        status_code=200
    )


@app.post("/proposals/{proposal_id}/edit_phone")
def edit_phone_save(proposal_id: int, request: Request, client_whatsapp: str = Form(""), db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    p = db.query(Proposal).filter(Proposal.id == proposal_id, Proposal.owner_id == user.id).first()
    if not p:
        return RedirectResponse("/dashboard", status_code=302)

    p.client_whatsapp = client_whatsapp.strip() or None
    db.add(p)
    db.commit()
    return RedirectResponse("/dashboard", status_code=302)


# ==========================
# PUBLIC PROPOSAL + TRACKING + ACCEPT
# ==========================
@app.get("/p/{public_id}", response_class=HTMLResponse)
def public_proposal(public_id: str, request: Request, db: Session = Depends(get_db)):
    p = db.query(Proposal).filter(Proposal.public_id == public_id).first()
    if not p:
        return HTMLResponse("Proposta não encontrada.", status_code=404)

    owner = db.query(User).filter(User.id == p.owner_id).first()
    base_url = base_url_from_request(request)

    # TRACK VIEW (evita contar refresh a cada segundo)
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
    # grava cookie de view
    resp.set_cookie(view_cookie, _now().isoformat(), max_age=60 * 60 * 24 * 30, samesite="lax")
    return resp


@app.post("/p/{public_id}/accept", response_class=HTMLResponse)
def accept_proposal(
    public_id: str,
    request: Request,
    name: str = Form(...),
    email: str = Form(""),  # agora é opcional
    db: Session = Depends(get_db),
):
    p = db.query(Proposal).filter(Proposal.public_id == public_id).first()
    if not p:
        return HTMLResponse("Proposta não encontrada.", status_code=404)

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

    return templates.TemplateResponse("accepted.html", {
        "request": request,
        "p": p,
        "owner": owner,
        "base_url": base_url
    })


@app.get("/p/{public_id}/pdf")
def public_pdf(public_id: str, request: Request, db: Session = Depends(get_db)):
    p = db.query(Proposal).filter(Proposal.public_id == public_id).first()
    if not p:
        return HTMLResponse("Proposta não encontrada.", status_code=404)

    user = db.query(User).filter(User.id == p.owner_id).first()
    accept_url = f"{base_url_from_request(request)}/p/{p.public_id}"

    pdf_bytes = generate_proposal_pdf({
        "client_name": p.client_name,
        "project_name": p.project_name,
        "description": p.description,
        "price": p.price,
        "deadline": p.deadline,
        "author_email": user.email if user else "",
        "author_name": user.display_name if user and user.display_name else "",
        "company_name": user.company_name if user and user.company_name else "",
        "phone": user.phone if user and user.phone else "",
        "is_pro": (user is not None and is_pro_active(user)),
        "validity_days": 7,
        "accept_url": accept_url,
    })

    filename = f"proposta_{p.client_name.replace(' ', '')}_{p.public_id}.pdf"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return Response(content=pdf_bytes, media_type="application/pdf", headers=headers)


@app.get("/proposals/{proposal_id}/pdf")
def download_pdf(proposal_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    p = db.query(Proposal).filter(
        Proposal.id == proposal_id,
        Proposal.owner_id == user.id
    ).first()
    if not p:
        return RedirectResponse("/dashboard", status_code=302)

    accept_url = f"{base_url_from_request(request)}/p/{p.public_id}"

    pdf_bytes = generate_proposal_pdf({
        "client_name": p.client_name,
        "project_name": p.project_name,
        "description": p.description,
        "price": p.price,
        "deadline": p.deadline,
        "author_email": user.email if user else "",
        "author_name": user.display_name if user and user.display_name else "",
        "company_name": user.company_name if user and user.company_name else "",
        "phone": user.phone if user and user.phone else "",
        "is_pro": is_pro_active(user),
        "validity_days": 7,
        "accept_url": accept_url,
    })

    filename = f"proposta_{p.client_name.replace(' ', '')}_{p.id}.pdf"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return Response(content=pdf_bytes, media_type="application/pdf", headers=headers)


# ==========================
# PROFILE
# ==========================
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

    db.add(user)
    db.commit()

    return templates.TemplateResponse("profile.html", {"request": request, "user": user, "saved": True, "error": None})


# ==========================
# BILLING / PRICING / STATIC PAGES
# ==========================
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


# ==========================
# ASAAS CUSTOMER
# ==========================
def ensure_asaas_customer(db: Session, user: User) -> str:
    if user.asaas_customer_id:
        return user.asaas_customer_id

    if not ASAAS_API_KEY:
        raise RuntimeError("ASAAS_API_KEY não configurado no Render.")

    name = user.display_name or user.company_name or user.email.split("@")[0]
    payload = {"name": name, "email": user.email}
    if user.cpf_cnpj:
        payload["cpfCnpj"] = user.cpf_cnpj

    r = requests.post(
        f"{asaas_api_base()}/customers",
        headers=asaas_headers(),
        json=payload,
        timeout=30,
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Erro Asaas ao criar customer: {r.status_code} - {r.text}")

    data = r.json()
    customer_id = data.get("id")
    if not customer_id:
        raise RuntimeError(f"Asaas não retornou customer id: {data}")

    user.asaas_customer_id = customer_id
    db.add(user)
    db.commit()
    return customer_id


# ==========================
# UPGRADE PRO (ASSINATURA ASAAS)
# ==========================
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
        return templates.TemplateResponse("profile.html", {
            "request": request,
            "user": user,
            "saved": False,
            "error": "Para assinar o PRO, preencha seu CPF/CNPJ no perfil e salve.",
        })

    try:
        customer_id = ensure_asaas_customer(db, user)
    except Exception as e:
        return HTMLResponse(f"Erro ao criar/obter customer no Asaas:<br><pre>{str(e)}</pre>", status_code=500)

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

    try:
        r = requests.post(
            f"{asaas_api_base()}/subscriptions",
            headers=asaas_headers(),
            json=payload,
            timeout=30,
        )
    except Exception as e:
        return HTMLResponse(f"Erro de conexão ao Asaas:<br><pre>{str(e)}</pre>", status_code=500)

    if r.status_code not in (200, 201):
        return HTMLResponse(f"Erro Asaas ao criar assinatura: {r.status_code}<br><pre>{r.text}</pre>", status_code=500)

    sub = r.json()
    sub_id = sub.get("id")
    if not sub_id:
        return HTMLResponse(f"Asaas não retornou subscription id.<br><pre>{sub}</pre>", status_code=500)

    user.asaas_subscription_id = sub_id
    db.add(user)
    db.commit()

    rp = requests.get(
        f"{asaas_api_base()}/subscriptions/{sub_id}/payments",
        headers=asaas_headers(),
        timeout=30,
    )

    if rp.status_code != 200:
        return HTMLResponse(f"Assinatura criada, mas não consegui listar cobranças: {rp.status_code}<br><pre>{rp.text}</pre>", status_code=500)

    payments = rp.json()
    data_list = payments.get("data") if isinstance(payments, dict) else None
    if not data_list:
        return HTMLResponse(f"Assinatura criada, mas ainda não veio payment.<br><pre>{payments}</pre>", status_code=500)

    first = data_list[0]
    invoice_url = first.get("invoiceUrl")
    if not invoice_url:
        return HTMLResponse(f"Não encontrei invoiceUrl no payment.<br><pre>{first}</pre>", status_code=500)

    return RedirectResponse(invoice_url, status_code=302)


# ==========================
# WEBHOOK ASAAS (AGORA NO LUGAR CERTO)
# ==========================
@app.post("/webhooks/asaas")
async def webhooks_asaas(request: Request, db: Session = Depends(get_db)):
    # valida token (se você configurou no Asaas)
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

    # tenta externalReference
    external_ref = ""
    if isinstance(payment, dict):
        external_ref = payment.get("externalReference") or ""
    if not external_ref and isinstance(subscription, dict):
        external_ref = subscription.get("externalReference") or ""

    # fallback: buscar subscription se vier id
    if not external_ref and isinstance(payment, dict) and payment.get("subscription"):
        sub_id = payment.get("subscription")
        try:
            rs = requests.get(
                f"{asaas_api_base()}/subscriptions/{sub_id}",
                headers=asaas_headers(),
                timeout=30,
            )
            if rs.status_code == 200:
                sj = rs.json()
                external_ref = sj.get("externalReference") or ""
        except Exception:
            pass

    if not external_ref.startswith("user_"):
        return {"ok": True}

    try:
        user_id = int(external_ref.replace("user_", ""))
    except Exception:
        return {"ok": True}

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return {"ok": True}

    # eventos que liberam PRO
    if event in ("PAYMENT_CONFIRMED", "PAYMENT_RECEIVED", "PAYMENT_APPROVED"):
        # MVP: libera por 32 dias a partir de agora
        paid_until = _now() + timedelta(days=32)
        set_user_pro_month(db, user, paid_until, subscription_id=user.asaas_subscription_id, customer_id=user.asaas_customer_id)
        return {"ok": True}

    return {"ok": True}