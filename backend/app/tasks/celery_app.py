# app/tasks/celery_app.py — replace entire file

import os
from celery import Celery, Task
from flask import Flask


def celery_init_app(app: Flask) -> Celery:

    class FlaskTask(Task):
        def __call__(self, *args, **kwargs):
            with app.app_context():
                return self.run(*args, **kwargs)

    redis_url: str = app.config.get("REDIS_URL", "redis://localhost:6379/0")

    celery_app = Celery(
        app.name,
        task_cls=FlaskTask,
        broker=redis_url,          # ← set directly in constructor
        backend=redis_url,         # ← set directly in constructor
    )

    celery_app.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        timezone="Asia/Kolkata",
        enable_utc=True,
        broker_connection_retry_on_startup=True,
    )

    celery_app.autodiscover_tasks([
        "app.tasks.apply_tasks",
        "app.tasks.email_tasks",
        "app.tasks.ml_tasks",
    ])

    return celery_app