"""Explicitly supported applicant-facing transports; no generic browser submission."""

from . import cato_greenhouse, secret_telaviv


def adapter(application):
    for provider in (secret_telaviv, cato_greenhouse):
        if provider.supported(application):
            return provider
    return None
