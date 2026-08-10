from aiogram import Dispatcher

from .start import router as start_router
from .auth import router as auth_router
from .upload import router as upload_router
from .clone import router as clone_router
from .drive_browser import router as drive_router
from .stats import router as stats_router
from .admin import router as admin_router


def register_all_routers(dp: Dispatcher):
    # Order matters: admin & auth first so their commands aren't shadowed
    dp.include_router(admin_router)
    dp.include_router(auth_router)
    dp.include_router(start_router)
    dp.include_router(upload_router)
    dp.include_router(clone_router)
    dp.include_router(drive_router)
    dp.include_router(stats_router)
