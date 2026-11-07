"""Public discovery defaults; no user profile or application data is used in queries."""

COUNTRIES = {
    "IL": "Israel",
    "UA": "Ukraine",
    "AT": "Austria",
    "BE": "Belgium",
    "BG": "Bulgaria",
    "HR": "Croatia",
    "CY": "Cyprus",
    "CZ": "Czechia",
    "DK": "Denmark",
    "EE": "Estonia",
    "FI": "Finland",
    "FR": "France",
    "DE": "Germany",
    "GR": "Greece",
    "HU": "Hungary",
    "IE": "Ireland",
    "IT": "Italy",
    "LV": "Latvia",
    "LT": "Lithuania",
    "LU": "Luxembourg",
    "MT": "Malta",
    "NL": "Netherlands",
    "PL": "Poland",
    "PT": "Portugal",
    "RO": "Romania",
    "SK": "Slovakia",
    "SI": "Slovenia",
    "ES": "Spain",
    "SE": "Sweden",
}


def discovery_queries() -> list[str]:
    return [
        f'"{country}" ("software engineer" OR "developer" OR "DevOps") careers jobs'
        for country in COUNTRIES.values()
    ]


def technology_roles() -> list[str]:
    return [
        "software engineer",
        "software developer",
        "backend",
        "back-end",
        "frontend",
        "front-end",
        "full stack",
        "full-stack",
        "devops",
        "sre",
        "data engineer",
        "data scientist",
        "machine learning",
        "security engineer",
        "qa engineer",
        "automation engineer",
        "platform engineer",
        "embedded",
        "firmware",
        "systems engineer",
        "python developer",
        "golang developer",
        "rust developer",
    ]
