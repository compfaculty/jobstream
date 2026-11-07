"""Catalogue page commits are independent of employer-site inspection queues."""

import hashlib

from django.db import transaction

from . import employer_discovery as discovery
from . import pituchim
from .company_feeds import CompanyRecord, FeedPage
from .company_registry import observe


@transaction.atomic
def save_batch(run_id, token, records, *, final=False, cursor=None, rejected=0):
    pair = discovery.locked(run_id, token)
    if pair is None:
        return False
    run, campaign = pair
    if not campaign.catalogue_mode:
        return False
    for record in records:
        result = observe(campaign, run, record)
        run.companies_created += int(result.created)
        run.companies_existing += int(not result.created and not result.rejected)
        run.rejected += int(result.rejected)
        run.unresolved += int(result.unresolved)
    if final:
        run.rejected += rejected
        campaign.query_pages = cursor if cursor else {"catalogue_complete": True}
        if campaign.provider == "pituchim":
            campaign.directory_page = (
                cursor.get("page", campaign.directory_page) if cursor else campaign.directory_page
            )
        campaign.save(update_fields=["query_pages", "directory_page"])
        run.retries, run.diagnostic = 0, ""
        discovery.release(run, delay=5)
    else:
        run.save()
    return True


def save_page(run_id, token, page: FeedPage):
    # An interruption before the final checkpoint replays already imported rows idempotently.
    batches = [page.records[i : i + 250] for i in range(0, len(page.records), 250)] or [[]]
    for index, records in enumerate(batches):
        if not save_batch(
            run_id,
            token,
            records,
            final=index == len(batches) - 1,
            cursor=page.cursor,
            rejected=page.rejected,
        ):
            return


def directory_page(companies, page, more):
    records = [
        CompanyRecord(
            company_key="website:" + hashlib.sha256(company.website.encode()).hexdigest(),
            record_key=company.website,
            name=company.name,
            source_url=pituchim.page_url(page),
            website=company.website,
            hiring=company.hiring,
            regional_status="target",
            regional_evidence="Pituchim Israeli-founded company directory",
        )
        for company in companies
    ]
    return FeedPage(records, {"page": page + 1} if more else {})
