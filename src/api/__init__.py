from fastapi import APIRouter
from .sqb import router as sqb_router

main_router = APIRouter()

main_router.include_router(sqb_router)