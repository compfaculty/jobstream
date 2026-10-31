"""Keep strict asset manifests while supporting Jazzmin's directory URL attribute."""

from urllib.parse import urljoin

from django.conf import settings
from whitenoise.storage import CompressedManifestStaticFilesStorage


class AdminStaticFilesStorage(CompressedManifestStaticFilesStorage):
    def url(self, name, force=False):
        # Jazzmin 3.0.5 renders this directory as data-theme-base even with its theme
        # chooser disabled. Directories cannot have manifest entries; files remain strict.
        if name == "vendor/bootswatch":
            return urljoin(settings.STATIC_URL, "vendor/bootswatch")
        return super().url(name, force=force)
