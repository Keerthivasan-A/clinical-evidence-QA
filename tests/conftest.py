"""Shared pytest setup."""
from settings import load_env

# Lets TEST_DATABASE_URL come from .env, like every other setting.
load_env()
