"""Explicitly supported applicant-facing transports; no generic browser submission."""

from . import camunda_ashby, cato_greenhouse, gitlab_greenhouse, secret_telaviv


def adapter(application):
    for provider in (secret_telaviv, cato_greenhouse, camunda_ashby, gitlab_greenhouse):
        if provider.supported(application):
            return provider
    return None


def browser_supported(application):
    return (
        cato_greenhouse.supported(application)
        or camunda_ashby.supported(application)
        or gitlab_greenhouse.supported(application)
    )
