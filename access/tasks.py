import logging

from celery import shared_task

from .views import sync_external_users

logger = logging.getLogger(__name__)


@shared_task(name="access.fetch_external_users")
def fetch_external_users_task():
    """Periodic task to refresh local user/mapping data from external accounting."""
    result = sync_external_users()
    if result.get("ok"):
        logger.info("External users sync completed", extra={"updated_count": result.get("updated_count", 0)})
    else:
        logger.error("External users sync failed", extra={"error": result.get("error")})

    return result
