"""Gunicorn entrypoint: `gunicorn wsgi:app`.

Kept out of hl_usdc_bot.app so importing the app factory never reads the
environment as a side effect.
"""

from hl_usdc_bot.app import create_app

app = create_app()
