from __future__ import annotations

import uuid

from fastapi import APIRouter
from pydantic import BaseModel

from packages.db.models import Product
from packages.db.session import session_scope

router = APIRouter()


class ProductOut(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    lifecycle: str


@router.get("", response_model=list[ProductOut])
def list_products() -> list[ProductOut]:
    with session_scope() as session:
        products = session.query(Product).order_by(Product.slug).all()
        return [ProductOut(id=p.id, slug=p.slug, name=p.name, lifecycle=p.lifecycle) for p in products]
