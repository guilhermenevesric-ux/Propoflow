from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime
from db import Base

import uuid

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    password_hash = Column(String(255), nullable=False)

    display_name = Column(String(255), nullable=True)
    company_name = Column(String(255), nullable=True)
    phone = Column(String(50), nullable=True)

    proposals = relationship("Proposal", back_populates="owner")

class Proposal(Base):
    __tablename__ = "proposals"
    id = Column(Integer, primary_key=True, index=True)
    public_id = Column(String(16), unique=True, index=True, nullable=False, default=lambda: uuid.uuid4().hex[:12])
    client_name = Column(String(255), nullable=False)
    project_name = Column(String(255), nullable=False)
    description = Column(Text, nullable=False)
    price = Column(String(50), nullable=False)
    deadline = Column(String(100), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    accepted_at = Column(DateTime, nullable=True)
    accepted_name = Column(String(255), nullable=True)
    accepted_email = Column(String(255), nullable=True)

    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    owner = relationship("User", back_populates="proposals")