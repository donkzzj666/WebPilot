"""Initialize only the business DB through numbered, transactional migrations."""
from .config import Settings
from .db import migrate


def initialize_business_storage(settings: Settings) -> dict:
    return migrate(settings.business_db)
