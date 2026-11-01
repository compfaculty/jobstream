"""Title-based role categories, evaluated in PostgreSQL before pagination."""

from dataclasses import dataclass

from django.db.models import Q


@dataclass(frozen=True)
class Category:
    slug: str
    label: str
    terms: str = ""

    @property
    def condition(self) -> Q:
        # PostgreSQL uses \m and \M for word boundaries (not Python's \b).
        return Q(title__iregex=rf"\m({self.terms})\M")


SPECIALIZED = (
    Category("backend", "Backend", r"back[ -]?end|server[ -]?side"),
    Category("frontend", "Frontend", r"front[ -]?end|client[ -]?side|web developer"),
    Category("full-stack", "Full Stack", r"full[ -]?stack"),
    Category("mobile", "Mobile", r"mobile|android|ios|flutter|react native"),
    Category(
        "devops",
        "DevOps & Infrastructure",
        r"dev[ -]?ops|devsecops|sre|site reliability|infrastructure|platform engineer"
        r"|cloud engineer|systems? administrat(or|ion)|network engineer|release engineer",
    ),
    Category(
        "security",
        "Security",
        r"security|cyber[ -]?security|infosec|appsec|devsecops|pentest(er|ing)?"
        r"|penetration test(er|ing)|threat|soc analyst",
    ),
    Category(
        "data-ai",
        "Data & AI",
        r"data|ai|ml|machine learning|deep learning|artificial intelligence"
        r"|analytics|business intelligence|computer vision|nlp|llm|mlops",
    ),
    Category("qa", "QA", r"qa|sdet|quality assurance|quality engineer|test(er|ing| engineer)"),
)
NON_ENGINEERING = (
    Category("product-design", "Product & Design", r"product|design(er)?|ux|ui"),
    Category(
        "business-operations",
        "Business & Operations",
        r"sales|marketing|finance|financial|account(ant|ing)?|account executive"
        r"|account manager|hr|human resources|recruit(er|ing|ment)|business"
        r"|operations|customer|support|legal|office|administrative|executive assistant",
    ),
)
GENERAL = Category("engineering", "General Engineering", r"engineer(ing)?|developer|programmer")
CATEGORIES = (
    Category("all", "All"),
    *SPECIALIZED,
    GENERAL,
    *NON_ENGINEERING,
    Category("other", "Other"),
)


def combined_condition(categories: tuple[Category, ...]) -> Q:
    condition = Q(pk__in=[])
    for category in categories:
        condition |= category.condition
    return condition


def category_filter(slug: str) -> Q:
    specialized = combined_condition(SPECIALIZED)
    if slug == GENERAL.slug:
        return GENERAL.condition & ~specialized
    if slug == "other":
        return ~(specialized | GENERAL.condition | combined_condition(NON_ENGINEERING))
    for category in (*SPECIALIZED, *NON_ENGINEERING):
        if category.slug == slug:
            return category.condition
    return Q()


def resolve_category(slug: str) -> str:
    return slug if any(category.slug == slug for category in CATEGORIES) else "all"
