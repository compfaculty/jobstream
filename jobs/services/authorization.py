from django.http import Http404

from jobs.models import Application, Document


def owned_application(user, pk: int) -> Application:
    try:
        return Application.objects.select_related("document", "job").get(pk=pk, owner=user)
    except Application.DoesNotExist:
        raise Http404 from None


def owned_document(user, pk: int) -> Document:
    try:
        return Document.objects.get(pk=pk, owner=user)
    except Document.DoesNotExist:
        raise Http404 from None
