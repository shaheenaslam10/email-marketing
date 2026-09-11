import os
import sys
import time
import threading
import logging
from django.apps import AppConfig

logger = logging.getLogger(__name__)


class OdkConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.odk'

    def ready(self):
        # Prevent starting thread during migrations, management commands, or tests
        is_manage_command = any(arg in sys.argv for arg in ['makemigrations', 'migrate', 'test', 'shell', 'collectstatic'])
        if is_manage_command:
            return

        # In runserver, RUN_MAIN == 'true' indicates the active reloader child worker
        if os.environ.get('RUN_MAIN') == 'true' or (not os.environ.get('DJANGO_AUTORELOAD') and 'runserver' not in sys.argv):
            self._start_periodic_sync_thread()

    def _start_periodic_sync_thread(self):
        def _background_worker():
            logger.info("ODK 5-minute background sync worker initialized.")
            # Wait 10 seconds after server startup before running first sync
            time.sleep(10)
            while True:
                try:
                    from .services import sync_all_odk_contacts
                    logger.info("Running automatic 5-minute ODK sync...")
                    res = sync_all_odk_contacts(trigger_source='SCHEDULED')
                    logger.info("Automatic 5-minute ODK sync completed: %s", res.get('message'))
                except Exception as e:
                    logger.warning("Automatic 5-minute ODK sync error: %s", str(e))

                try:
                    from apps.reminders.tasks import task_check_scheduled_campaigns, task_check_scheduled_reminders
                    task_check_scheduled_campaigns()
                    task_check_scheduled_reminders()
                except Exception as sch_err:
                    logger.debug("Periodic scheduled dispatch notice: %s", sch_err)

                time.sleep(300)  # Wait 300 seconds (5 minutes)

        thread = threading.Thread(target=_background_worker, daemon=True, name="ODK_5Min_Sync_Worker")
        thread.start()
