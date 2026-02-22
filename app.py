from fastapi import FastAPI, Request, Form, Depends, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from passlib.hash import pbkdf2_sha256
from datetime import datetime
import os
import requests
import subprocess
import sys

from db import SessionLocal, engine, Base
from models import User, Proposal
from pdf_gen import generate_proposal_pdf


# ====== roda migração leve (SQLite/Postgres) ======
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


COOKIE_NAME = "user_id"

MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "").strip()
APP_BASE_URL = os.getenv("APP_BASE_URL", "").strip().rstrip("/")
MP_API = "https://api.mercadopago.com"


def mp_headers():
    return {
        "Authorization": f"Bearer {MP_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }


def get_current_user(request: Request, db: Session):
    user_id = request.cookies.get(COOKIE_NAME)
    if not user_id or user_id in ("None", "null", ""):
        return None
    try:
        user_id_int = int(user_id)
    except ValueError:
        return None
    return db.query(User).filter(User.id == user_id_int).first()


def set_user_pro(db: Session, user: User, preapproval_id: str | None = None):
    user.plan = "pro"
    user.proposal_limit = 999999
    user.delete_credits = 999999
    user.plan_updated_at = datetime.utcnow()
    if preapproval_id:
        user.mp_last_preapproval_id = preapproval_id
    db.add(user)
    db.commit()


@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    user_id = request.cookies.get(COOKIE_NAME)
    user = get_current_user(request, db)

    if not user:
        resp = RedirectResponse("/login", status_code=302)
        if user_id in ("None", "null", ""):
            resp.delete_cookie(COOKIE_NAME)
        return resp

    return RedirectResponse("/dashboard", status_code=302)


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

    resp = RedirectResponse("/dashboard", status_code=302)
    resp.set_cookie(COOKIE_NAME, str(user.id), httponly=True)
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

    resp = RedirectResponse("/dashboard", status_code=302)
    resp.set_cookie(COOKIE_NAME, str(user.id), httponly=True)
    return resp


@app.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie(COOKIE_NAME)
    return resp


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

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "user": user,
        "proposals": proposals,
        "total": total,
        "accepted": accepted,
        "rate": rate,
        "status": status,
    })


@app.get("/proposals/new", response_class=HTMLResponse)
def new_proposal_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse("new_proposal.html", {"request": request, "error": None})


@app.post("/proposals/new")
def create_proposal(
    request: Request,
    client_name: str = Form(...),
    project_name: str = Form(...),
    description: str = Form(...),
    price: str = Form(...),
    deadline: str = Form(...),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    count = db.query(Proposal).filter(Proposal.owner_id == user.id).count()
    if count >= (user.proposal_limit or 5):
        proposals = (
            db.query(Proposal)
            .filter(Proposal.owner_id == user.id)
            .order_by(Proposal.created_at.desc())
            .all()
        )

        total = count
        accepted = db.query(Proposal).filter(
            Proposal.owner_id == user.id,
            Proposal.accepted_at.isnot(None)
        ).count()
        rate = round((accepted / total) * 100) if total else 0

        return templates.TemplateResponse("dashboard.html", {
            "request": request,
            "user": user,
            "proposals": proposals,
            "total": total,
            "accepted": accepted,
            "rate": rate,
            "status": "all",
            "error": f"Você atingiu o limite do plano gratuito ({user.proposal_limit or 5} propostas).",
            "show_upgrade": True,
        })

    p = Proposal(
        client_name=client_name.strip(),
        project_name=project_name.strip(),
        description=description.strip(),
        price=price.strip(),
        deadline=deadline.strip(),
        owner_id=user.id,
    )
    db.add(p)
    db.commit()
    db.refresh(p)

    return RedirectResponse(f"/proposals/{p.id}/pdf", status_code=302)


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

    # Free: só 1 exclusão via delete_credits
    if user.plan == "free":
        credits = user.delete_credits or 0
        if credits <= 0:
            proposals = (
                db.query(Proposal)
                .filter(Proposal.owner_id == user.id)
                .order_by(Proposal.created_at.desc())
                .all()
            )
            total = db.query(Proposal).filter(Proposal.owner_id == user.id).count()
            accepted = db.query(Proposal).filter(
                Proposal.owner_id == user.id,
                Proposal.accepted_at.isnot(None)
            ).count()
            rate = round((accepted / total) * 100) if total else 0

            return templates.TemplateResponse("dashboard.html", {
                "request": request,
                "user": user,
                "proposals": proposals,
                "total": total,
                "accepted": accepted,
                "rate": rate,
                "status": "all",
                "error": "No plano gratuito você só pode excluir 1 proposta. Faça upgrade para excluir ilimitado.",
                "show_upgrade": True,
            })

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
        project_name=original.project_name,
        description=original.description,
        price=original.price,
        deadline=original.deadline,
        owner_id=user.id
    )

    db.add(new_p)
    db.commit()

    return RedirectResponse("/dashboard", status_code=302)


@app.post("/p/{public_id}/accept", response_class=HTMLResponse)
def accept_proposal(
    public_id: str,
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    db: Session = Depends(get_db),
):
    p = db.query(Proposal).filter(Proposal.public_id == public_id).first()
    if not p:
        return HTMLResponse("Proposta não encontrada.", status_code=404)

    if p.accepted_at is not None:
        owner = db.query(User).filter(User.id == p.owner_id).first()
        base_url = str(request.base_url).rstrip("/")
        return templates.TemplateResponse("accepted.html", {
            "request": request,
            "p": p,
            "owner": owner,
            "base_url": base_url
        })

    p.accepted_at = datetime.utcnow()
    p.accepted_name = name.strip()
    p.accepted_email = email.strip()
    db.commit()
    db.refresh(p)

    owner = db.query(User).filter(User.id == p.owner_id).first()
    base_url = str(request.base_url).rstrip("/")
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
    })

    filename = f"proposta_{p.client_name.replace(' ', '_')}_{p.public_id}.pdf"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return Response(content=pdf_bytes, media_type="application/pdf", headers=headers)


@app.get("/p/{public_id}", response_class=HTMLResponse)
def public_proposal(public_id: str, request: Request, db: Session = Depends(get_db)):
    p = db.query(Proposal).filter(Proposal.public_id == public_id).first()
    if not p:
        return HTMLResponse("Proposta não encontrada.", status_code=404)

    owner = db.query(User).filter(User.id == p.owner_id).first()
    base_url = str(request.base_url).rstrip("/")

    return templates.TemplateResponse("proposal_public.html", {
        "request": request,
        "p": p,
        "owner": owner,
        "base_url": base_url
    })


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
    })

    filename = f"proposta_{p.client_name.replace(' ', '_')}_{p.id}.pdf"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return Response(content=pdf_bytes, media_type="application/pdf", headers=headers)


@app.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    return templates.TemplateResponse("profile.html", {"request": request, "user": user, "saved": False})


@app.post("/profile")
def profile_save(
    request: Request,
    display_name: str = Form(""),
    company_name: str = Form(""),
    phone: str = Form(""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    user.display_name = display_name.strip() or None
    user.company_name = company_name.strip() or None
    user.phone = phone.strip() or None
    db.add(user)
    db.commit()

    return templates.TemplateResponse("profile.html", {"request": request, "user": user, "saved": True})


@app.get("/pricing", response_class=HTMLResponse)
def pricing(request: Request):
    return templates.TemplateResponse("pricing.html", {"request": request})


# ====== UPGRADE PRO (ASSINATURA) ======
@app.get("/upgrade/pro")
def upgrade_pro(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    if not MP_ACCESS_TOKEN:
        return HTMLResponse("MP_ACCESS_TOKEN não configurado no Render.", status_code=500)

    base_url = APP_BASE_URL or str(request.base_url).rstrip("/")

    payload = {
        "reason": "PropoFlow Pro (assinatura mensal)",
        "external_reference": f"user_{user.id}",
        "payer_email": user.email,
        "back_url": f"{base_url}/billing/success",
        "auto_recurring": {
            "frequency": 1,
            "frequency_type": "months",
            "transaction_amount": 19.0,
            "currency_id": "BRL",
        },
        "notification_url": f"{base_url}/webhooks/mercadopago",
    }

    r = requests.post(
        f"{MP_API}/preapproval",
        headers=mp_headers(),
        json=payload,
        timeout=30,
    )

    if r.status_code not in (200, 201):
        return HTMLResponse(f"Erro Mercado Pago: {r.status_code}<br><pre>{r.text}</pre>", status_code=500)

    data = r.json()
    init_point = data.get("init_point")
    if not init_point:
        return HTMLResponse(f"Mercado Pago não retornou init_point.<br><pre>{data}</pre>", status_code=500)

    return RedirectResponse(init_point, status_code=302)


@app.get("/billing/success", response_class=HTMLResponse)
def billing_success(request: Request):
    return HTMLResponse("""
    <div style="font-family:system-ui; padding:24px;">
      <h2>Pagamento recebido! ✅</h2>
      <p>Se sua assinatura foi confirmada, seu PRO será liberado automaticamente em instantes.</p>
      <p><a href="/dashboard">Voltar ao dashboard</a></p>
    </div>
    """)


# ====== WEBHOOK ÚNICO (ASSINATURA) ======
@app.post("/webhooks/mercadopago")
async def webhooks_mercadopago(request: Request, db: Session = Depends(get_db)):
    # MP às vezes manda query params: ?data.id=123&type=payment
    q_id = request.query_params.get("data.id")
    q_type = request.query_params.get("type")

    body = {}
    try:
        body = await request.json()
    except Exception:
        body = {}

    data = body.get("data") if isinstance(body, dict) else {}
    b_id = None
    if isinstance(data, dict):
        b_id = data.get("id")

    resource_id = str(b_id or q_id or "").strip()
    event_type = (body.get("type") if isinstance(body, dict) else None) or q_type

    if not resource_id or not MP_ACCESS_TOKEN:
        return {"ok": True}

    event_type = (event_type or "").lower()

    # Para assinatura, o que importa é "preapproval"/"subscription_preapproval".
    # Se vier como payment, a gente ignora (não quebra).
    if "preapproval" not in event_type:
        return {"ok": True}

    # Busca assinatura
    r = requests.get(
        f"{MP_API}/preapproval/{resource_id}",
        headers=mp_headers(),
        timeout=30,
    )
    if r.status_code != 200:
        return {"ok": True}

    sub = r.json()
    status = (sub.get("status") or "").lower()
    external_reference = sub.get("external_reference") or ""

    # status "authorized" = assinatura ativa
    if status == "authorized" and external_reference.startswith("user_"):
        try:
            user_id = int(external_reference.replace("user_", ""))
        except Exception:
            return {"ok": True}

        user = db.query(User).filter(User.id == user_id).first()
        if user:
            # evita reprocessar sem necessidade
            if user.plan == "pro" and user.mp_last_preapproval_id == resource_id:
                return {"ok": True}
            set_user_pro(db, user, preapproval_id=resource_id)

    return {"ok": True}