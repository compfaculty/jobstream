from urllib.parse import parse_qs, urlsplit

import pytest
from bs4 import BeautifulSoup

from jobs.models import Company, Job, SavedJob
from jobs.services.categories import CATEGORIES, category_filter

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Senior BACK-END Engineer", {"backend"}),
        ("Front End Developer", {"frontend"}),
        ("Fullstack Engineer", {"full-stack"}),
        ("iOS Developer", {"mobile"}),
        ("Site Reliability Engineer", {"devops"}),
        ("DevSecOps Engineer", {"devops", "security"}),
        ("Backend Security Engineer", {"backend", "security"}),
        ("Machine Learning Engineer", {"data-ai"}),
        ("QA Automation Engineer", {"qa"}),
        ("Software Engineer", {"engineering"}),
        ("UX Designer", {"product-design"}),
        ("Account Manager", {"business-operations"}),
        ("Chair Repair Specialist", {"other"}),
        ("Frontendish Specialist", {"other"}),
        ("", {"other"}),
    ],
)
def test_title_categories(job, title, expected):
    job.title = title
    job.description = "Backend Security Data AI QA sales"
    job.save()
    actual = {
        category.slug
        for category in CATEGORIES
        if category.slug != "all"
        and Job.objects.filter(pk=job.pk).filter(category_filter(category.slug)).exists()
    }
    assert actual == expected


def test_all_fallback_and_closed_jobs(authenticated, job):
    closed = Job.objects.create(
        canonical_url="https://example.com/closed", title="Backend", active=False
    )
    for category in ("", "all", "unknown", "<script>"):
        response = authenticated.get("/", {"category": category})
        assert list(response.context["page"]) == [closed, job]
        assert response.context["selected_category"] == "all"


def test_category_filters_and_links(authenticated, job, user, other):
    company = Company.objects.create(name="Example")
    job.title = "Backend Security Engineer"
    job.company_profile = company
    job.save()
    SavedJob.objects.create(owner=user, job=job)
    unseen = Job.objects.create(
        canonical_url="https://example.com/other-user",
        title=job.title,
        company_profile=company,
        location=job.location,
        description=job.description,
    )
    SavedJob.objects.create(owner=other, job=unseen)
    params = {
        "category": "backend",
        "q": "Python",
        "location": "Tel Aviv",
        "company": str(company.pk),
        "saved": "1",
        "page": "2",
    }
    response = authenticated.get("/", params, HTTP_HX_REQUEST="true")
    assert list(response.context["page"]) == [job]
    assert b"Find your next job" not in response.content
    soup = BeautifulSoup(response.content, "html.parser")
    nav = soup.find("nav", attrs={"aria-label": "Job categories"})
    assert nav.find(attrs={"aria-current": "page"}).text == "Backend"
    assert soup.find("input", attrs={"name": "category"})["form"] == "job-search"
    security = nav.find("a", string="Security")
    values = parse_qs(urlsplit(security["href"]).query)
    assert values == {key: [value] for key, value in params.items() if key != "page"} | {
        "category": ["security"],
    }
    assert security["hx-get"] == security["href"]
    assert (
        authenticated.get("/", {**params, "category": "mobile"}).context["page"].paginator.count
        == 0
    )


def test_category_pagination_and_clear(authenticated):
    Job.objects.bulk_create(
        [
            Job(canonical_url=f"https://example.com/backend/{i}", title="Backend Engineer")
            for i in range(26)
        ]
        + [Job(canonical_url="https://example.com/mobile", title="Mobile Engineer")]
    )
    response = authenticated.get("/", {"category": "backend", "page": 2})
    assert response.context["page"].paginator.count == 26
    assert len(response.context["page"]) == 1
    soup = BeautifulSoup(response.content, "html.parser")
    previous = soup.find("nav", attrs={"aria-label": "Pages"}).find("a")
    assert parse_qs(urlsplit(previous["href"]).query) == {"category": ["backend"], "page": ["1"]}
    empty = authenticated.get("/", {"category": "security"})
    assert b"Clear filters" in empty.content
