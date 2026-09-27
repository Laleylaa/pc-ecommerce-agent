from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.repositories.catalog import CatalogRepository
from app.schemas.catalog import ProductSearchRequest, ProductSearchResponse

router = APIRouter(prefix="/catalog", tags=["catalog"])


@router.post("/search", response_model=ProductSearchResponse)
async def search_products(
    request: ProductSearchRequest, session: AsyncSession = Depends(get_session)
) -> ProductSearchResponse:
    products = await CatalogRepository(session).search_products(request)
    return ProductSearchResponse(products=products)
