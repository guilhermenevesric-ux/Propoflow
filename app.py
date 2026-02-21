from fastapi import FastAPI, Request, Form, Depends, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from passlib.hash import bcrypt
from fastapi.staticfiles import StaticFiles
from datetime import datetime

from db import SessionLocal, engine, Base
from models import User, Proposal
from pdf_gen import generate_proposal_pdf

Base.metadata.create_all(bind=engine)
import subprocess, sys, os

try:
    subprocess.run([sys.executable, "migrate.py"], check=False)
except Exception:
    pass

app = FastAPI()
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# Sessão ultra simples via cookie (MVP)
COOKIE_NAME = "user_id"

def get_current_user(request: Request, db: Session):
    user_id = request.cookies.get(COOKIE_NAME)
    if not user_id:
        return None
    return db.query(User).filter(User.id == int(user_id)).first()

@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return RedirectResponse("/dashboard", status_code=302)

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})

@app.post("/login")
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db)
):
    # bcrypt limita 72 bytes
    if len(password.encode("utf-8")) > 72:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Senha muito longa. Use até 72 caracteres."}
        )

    user = db.query(User).filter(User.email == email.strip().lower()).first()
    if not user or not bcrypt.verify(password, user.password_hash):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Email ou senha inválidos."}
        )

    resp = RedirectResponse("/dashboard", status_code=302)
    resp.set_cookie(COOKIE_NAME, str(user.id), httponly=True)
    return resp


@app.post("/register")
def register(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db)
):
    # bcrypt limita 72 bytes
    if len(password.encode("utf-8")) > 72:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Senha muito longa. Use até 72 caracteres."}
        )

    email = email.strip().lower()
    if db.query(User).filter(User.email == email).first():
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Esse email já existe. Faça login."}
        )

    user = User(
        email=email,
        password_hash=pbkdf2_sha256.hash(password),
        proposal_limit=5,
        plan="free"
    )

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

    rate = 0
    if total > 0:
        rate = round((accepted / total) * 100)

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "user": user,
        "proposals": proposals,
        "total": total,
        "accepted": accepted,
        "rate": rate,
        "status": status
    })


@app.post("/p/{public_id}/accept", response_class=HTMLResponse)
def accept_proposal(
    public_id: str,
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    db: Session = Depends(get_db)
):
    p = db.query(Proposal).filter(Proposal.public_id == public_id).first()
    if not p:
        return HTMLResponse("Proposta não encontrada.", status_code=404)

    # Se já foi aceita, não aceita de novo (bloqueia duplicado)
    if p.accepted_at is not None:
        owner = db.query(User).filter(User.id == p.owner_id).first()
        base_url = str(request.base_url).rstrip("/")
        return templates.TemplateResponse("accepted.html", {
            "request": request,
            "p": p,
            "owner": owner,
            "base_url": base_url
        })

    # Registra aceite
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
    db: Session = Depends(get_db)
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    count = db.query(Proposal).filter(Proposal.owner_id == user.id).count()

    if count >= user.proposal_limit:
        return templates.TemplateResponse(
            "dashboard.html",
            {
                "request": request,
                "user": user,
                "proposals": db.query(Proposal).filter(Proposal.owner_id == user.id).all(),
                "error": "Você atingiu o limite do plano gratuito."
            }
        )

    p = Proposal(
        client_name=client_name.strip(),
        project_name=project_name.strip(),
        description=description.strip(),
        price=price.strip(),
        deadline=deadline.strip(),
        owner_id=user.id
    )
    db.add(p)
    db.commit()
    db.refresh(p)

    return RedirectResponse(f"/proposals/{p.id}/pdf", status_code=302)

  # se ainda não tiver no topo


@app.get("/p/{public_id}/pdf")
def public_pdf(public_id: str, request: Request, db: Session = Depends(get_db)):
    p = db.query(Proposal).filter(Proposal.public_id == public_id).first()
    if not p:
        return HTMLResponse("Proposta não encontrada.", status_code=404)

    # aqui ainda usamos o dono pra "Emitente"
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

    p = db.query(Proposal).filter(Proposal.id == proposal_id, Proposal.owner_id == user.id).first()
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
    db: Session = Depends(get_db)
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
    return templates.TemplateResponse(
        "pricing.html",
        {"request": request}
    )








