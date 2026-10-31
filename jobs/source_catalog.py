"""Reviewed source references from compfaculty/jf; no crawling is enabled by this catalog."""

from dataclasses import dataclass


@dataclass(frozen=True)
class SourceDefinition:
    name: str
    adapter: str
    url: str
    notes: str
    max_items: int = 100
    employer_name: str = ""
    authoritative: bool = False
    max_response_bytes: int = 2 * 1024 * 1024


COMPANY_REFERENCES = (
    ("40Seas", "https://www.40seas.com/careers#positions"),
    ("Agrematch", "https://www.agrematch.com/careers"),
    ("Agora Real", "https://agorareal.com/careers/"),
    ("Aidoc", "https://www.aidoc.com/about/careers/"),
    ("AI21", "https://www.ai21.com/careers/"),
    ("Akeyless", "https://www.akeyless.io/careers/#positions"),
    ("Allot", "https://www.allot.com/careers/search/"),
    ("Amdocs", "https://jobs.amdocs.com/careers"),
    ("Anecdotes", "https://www.anecdotes.ai/careers"),
    ("Arbe Robotics", "https://arberobotics.com/career/"),
    ("Audiocodes", "https://www.audiocodes.com/careers/positions?countryGroup=Israel"),
    ("Axonius", "https://www.axonius.com/company/careers/open-jobs"),
    ("Buildots", "https://buildots.com/careers/co/development/"),
    ("C2A Security", "https://c2a-sec.com/careers/"),
    ("Carbyne", "https://carbyne.com/company/careers/"),
    ("Cato Networks", "https://www.catonetworks.com/careers/"),
    ("Check Point", "https://careers.checkpoint.com/index.php?m=cpcareers&a=search"),
    ("Double AI", "https://doubleai.com/careers/"),
    ("DriveNets", "https://drivenets.com/careers/"),
    ("Gett", "https://www.gett.com/careers/?location=tel-aviv-israel#open-positions"),
    ("Hailo", "https://hailo.ai/company-overview/careers/"),
    ("JFrog", "https://join.jfrog.com/positions/?gh_office=25868"),
    ("Kaltura", "https://corp.kaltura.com/company/careers/"),
    ("Kornit", "https://careers.kornit.com/all-positions/"),
    ("Mobileye", "https://careers.mobileye.com/jobs"),
    ("Noma Security", "https://noma.security/careers/"),
    ("ParaZero", "https://parazero.com/careers/"),
    ("Pentera", "https://pentera.io/careers/"),
    ("Perion", "https://perion.com/careers/"),
    ("Seemplicity", "https://seemplicity.io/about/careers/"),
    ("Ceragon Networks", "https://www.ceragon.com/about-ceragon/careers"),
    ("Varonis", "https://careers.varonis.com/"),
    (
        "Verint",
        "https://fa-epcb-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX/jobs?location=Israel",
    ),
    ("Cognyte", "https://www.cognyte.com/careers/"),
    ("CyberArk", "https://www.cyberark.com/careers/all-job-openings/"),
    ("Cymulate", "https://cymulate.com/careers/#open-positions"),
    ("D-Fend Solutions", "https://d-fendsolutions.com/about-us/careers/"),
    ("Elsight", "https://www.elsight.com/careers/"),
    ("Firebolt", "https://www.firebolt.io/careers"),
    ("Gilat Satellite Networks", "https://www.gilat.com/career/#petah-tikva-israel"),
    ("Global-e", "https://www.global-e.com/careers/"),
    ("Idomoo", "https://www.idomoo.com/careers/#open-positions"),
    ("Komodor", "https://komodor.com/careers/#open-positions-title"),
    ("LayerX Security", "https://layerxsecurity.com/careers"),
    ("Mobilicom", "https://mobilicom.com/careers/"),
)


def definitions() -> list[SourceDefinition]:
    values = [
        SourceDefinition(
            "GitLab careers",
            "greenhouse",
            "https://boards-api.greenhouse.io/v1/boards/gitlab/jobs?content=true",
            "Public employer Greenhouse feed verified on 2026-10-31: 238 jobs; "
            "careers page references Greenhouse. Review before enabling.",
            max_items=500,
            employer_name="GitLab",
            authoritative=True,
            max_response_bytes=8 * 1024 * 1024,
        ),
        SourceDefinition(
            "Secret Tel Aviv",
            "secret_telaviv",
            "https://jobs.secrettelaviv.com/",
            "JF public HTML adapter; review live compatibility.",
            10,
        ),
        SourceDefinition(
            "Telfed",
            "telfed",
            "https://www.telfed.org.il/job-board/",
            "JF public HTML adapter; may require browser access, which is not bypassed.",
            10,
        ),
        *[
            SourceDefinition(
                f"DOU — {category}",
                "rss",
                f"https://jobs.dou.ua/vacancies/feeds/?category={category}",
                "RSS reference from JF; previously commented out.",
            )
            for category in ("Python", "AI/ML", "Golang", "DevOps")
        ],
        SourceDefinition(
            "Jobicy",
            "rss",
            "https://jobicy.com/feed/job_feed",
            "RSS reference from JF; previously commented out.",
        ),
        SourceDefinition(
            "Real Work From Anywhere",
            "realwork",
            "https://www.realworkfromanywhere.com/rss.xml",
            "RSS plus bounded job-detail enrichment; previously commented out in JF.",
            10,
        ),
    ]
    specialized = {
        "40Seas": "40seas",
        "Agrematch": "agrematch",
        "AI21": "ai21",
        "Akeyless": "akeyless",
        "Audiocodes": "audiocodes",
    }
    for name, url in COMPANY_REFERENCES:
        adapter = specialized.get(name, "jsonld")
        notes = (
            "Dedicated static parser ported from JF; live compatibility needs review."
            if name in specialized
            else (
                "Reference only: JSON-LD adapter needs embedded JobPosting data. "
                "JS/ATS integrations are not yet implemented for this company."
            )
        )
        if name in {"40Seas", "Agrematch"}:
            notes += (
                " Legacy URL returned HTTP 404 when checked on 2026-10-31; "
                "update URL before enabling."
            )
        values.append(
            SourceDefinition(name, adapter, url, notes, 10 if name in specialized else 100)
        )
    return values
