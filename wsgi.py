"""Gunicorn entry point.

Run with EXACTLY ONE worker (the poller/rules engine/janitor live in-process):
    gunicorn wsgi:app --workers 1 --threads 4 --bind 0.0.0.0:7170
"""
from app import create_app

app = create_app()
