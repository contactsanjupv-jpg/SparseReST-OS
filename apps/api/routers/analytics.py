from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Query

from packages.core.funnel import funnel_report_for_product
from packages.db.models import Product
from packages.db.session import session_scope

router = APIRouter()


@router.get("/funnel")
def funnel(product_id: uuid.UUID, window_days: int = Query(14, ge=3, le=90),
           baseline_days: int = Query(28, ge=7, le=180), maturity_days: int = Query(3, ge=0, le=14)) -> dict:
    """Stage-to-stage conversion for the recent cohort vs. its own trailing baseline."""
    with session_scope() as session:
        product = session.get(Product, product_id)
        if product is None:
            raise HTTPException(status_code=404, detail="Product not found")
        slug = product.slug
    report = funnel_report_for_product(product_id, window_days=window_days, baseline_days=baseline_days, maturity_days=maturity_days)
    return {"product_id": str(product_id), "product_slug": slug, **report}
