"""A tiny Django blog. Fixwire is set up at the bottom of this file."""

import os
from typing import Any

import fixwire

DEBUG = os.environ.get("DEBUG") == "1"
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-not-secret")
ALLOWED_HOSTS = ["*"]
ROOT_URLCONF = "blog.urls"
INSTALLED_APPS: list[str] = []
DATABASES: dict[str, dict[str, Any]] = {}
MIDDLEWARE = [
    # First, so every request (and every other middleware) runs in its scope.
    "fixwire.integrations.django.FixwireMiddleware",
    "django.middleware.common.CommonMiddleware",
]
LOGGING_CONFIG = None

# Fixwire: errors in views are reported with the request and the URL
# pattern; logged errors become events. Without FIXWIRE_DSN nothing is sent.
fixwire.init(
    dsn=os.environ.get("FIXWIRE_DSN"),
    release=os.environ.get("RELEASE", "blog@1.0.0"),
    environment=os.environ.get("ENVIRONMENT", "development"),
    # A trace per request, named after its URL pattern.
    traces_sample_rate=float(os.environ.get("TRACES_SAMPLE_RATE", "1.0")),
)
