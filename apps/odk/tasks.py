from celery import shared_task
import logging
from .services import sync_all_odk_contacts

logger = logging.getLogger(__name__)


@shared_task(name='apps.odk.tasks.task_sync_odk_periodic')
def task_sync_odk_periodic():
    """Periodic Celery Beat task that synchronizes ODK forms and datasets every 5 minutes."""
    logger.info("Executing scheduled 5-minute ODK sync task...")
    try:
        result = sync_all_odk_contacts(trigger_source='SCHEDULED')
        logger.info("ODK periodic sync completed: %s", result.get('message'))
        return result
    except Exception as e:
        logger.error("Error during scheduled ODK sync: %s", str(e), exc_info=True)
        return {'status': 'error', 'message': str(e)}
