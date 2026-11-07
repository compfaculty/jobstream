from django.contrib.postgres.search import SearchQuery, SearchRank
from django.db.models import F

from jobs.models import Job


def search_jobs(
    query: str = "", location: str = "", company: str = "", *, include_inactive: bool = False
):
    jobs = Job.objects.all().select_related("company_profile").prefetch_related("origins__source")
    if not include_inactive:
        jobs = jobs.filter(active=True)
    if company:
        if not company.isascii() or not company.isdecimal() or len(company) > 18:
            return jobs.none()
        jobs = jobs.filter(company_profile_id=int(company))
    if query.strip():
        search = SearchQuery(query[:200], search_type="websearch", config="english")
        jobs = (
            jobs.filter(search_document=search)
            .annotate(rank=SearchRank(F("search_document"), search))
            .order_by("-rank", "-pk")
        )
    if location:
        jobs = jobs.filter(location__icontains=location[:100])
    return jobs
