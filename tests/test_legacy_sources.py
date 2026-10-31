from datetime import UTC, datetime

import pytest

from jobs.services.sources import ADAPTERS, Listing, RSSAdapter, normalize


def test_jobicy_namespace_content_and_rss_date():
    data = b"""<rss version="2.0" xmlns:job_listing="https://jobicy.com"
    xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><item>
    <title>Engineer</title><link>https://example.com/jobs/1</link>
    <pubDate>Fri, 30 Oct 2026 12:30:00 +0200</pubDate>
    <job_listing:company>Company</job_listing:company>
    <job_listing:location>Remote</job_listing:location>
    <description>Short</description>
    <content:encoded><![CDATA[<p>Full description</p>]]></content:encoded>
    </item></channel></rss>"""
    result = RSSAdapter().discover(data, "https://example.com/feed", 1)[0]
    assert result.company == "Company"
    assert result.location == "Remote"
    assert result.description == "Full description"
    assert result.posted_at == datetime(2026, 10, 30, 10, 30, tzinfo=UTC)


def test_atom_updated_date_and_content():
    data = b"""<feed xmlns="http://www.w3.org/2005/Atom"><title>Jobs</title>
    <entry><title>Engineer</title><link href="https://example.com/job"/>
    <updated>2026-10-30T10:00:00Z</updated><content type="html">&lt;b&gt;Python&lt;/b&gt;</content>
    </entry></feed>"""
    result = RSSAdapter().discover(data, "https://example.com/feed", 1)[0]
    assert result.description == "Python"
    assert result.posted_at == datetime(2026, 10, 30, 10, tzinfo=UTC)


@pytest.mark.parametrize(
    "adapter,base,path,section,company",
    [
        (
            "40seas",
            "https://www.40seas.com/careers",
            "/open-positions/backend",
            "position-rich",
            "40Seas",
        ),
        (
            "ai21",
            "https://www.ai21.com/careers/",
            "/careers/backend/AB.123/",
            "career__details-item",
            "AI21",
        ),
        (
            "audiocodes",
            "https://www.audiocodes.com/careers/positions",
            "/careers/positions/backend",
            "section__body sec-body",
            "AudioCodes",
        ),
        (
            "akeyless",
            "https://www.akeyless.io/careers/",
            "https://www.comeet.com/jobs/akeyless/AB.123/backend",
            "userDesignedContent company-description",
            "Akeyless",
        ),
    ],
)
def test_company_profiles(adapter, base, path, section, company):
    parser = ADAPTERS[adapter]
    html = (
        f'<a href="{path}">Engineer</a><a href="https://evil.example/jobs/foo">Bad</a>'
        f'<a href="{path}">Duplicate</a>'
    ).encode()
    found = parser.discover(html, base, 10)
    assert len(found) == 1
    assert found[0].company == company
    details = f'<h1>Senior Engineer</h1><div class="{section}"><p>Python</p></div>'.encode()
    metadata = parser.metadata(found[0], details)
    assert metadata.title == "Senior Engineer"
    assert metadata.company == company
    assert "Python" in metadata.description
    with pytest.raises(ValueError):
        parser.metadata(found[0], b"<html>Access challenge</html>")


def test_agrematch_inline_jobs_have_distinct_stable_urls():
    data = b"""<div class="job_pos_div_f"><h2 class="open_pos_title sub_pos">Engineer</h2>
    <div class="pos_points">Python</div></div>
    <div class="job_pos_div_f"><h2 class="open_pos_title sub_pos">Scientist</h2>
    <div class="pos_points">Research</div></div>"""
    parser = ADAPTERS["agrematch"]
    first = parser.discover(data, "https://example.com/careers#positions", 10)
    second = parser.discover(data, "https://example.com/careers#positions", 10)
    assert len({normalize(item)["canonical_url"] for item in first}) == 2
    assert first == second
    assert first[0].apply_url == "https://example.com/careers#positions"
    assert len(parser.discover(data, "https://example.com/careers", 1)) == 1


def test_realwork_detail_enrichment_preserves_feed_date_and_rejects_unsafe_apply():
    listing = Listing(
        "https://www.realworkfromanywhere.com/jobs/1",
        "Feed title",
        posted_at=datetime(2026, 10, 30, tzinfo=UTC),
    )
    html = b"""<main><h1>Backend engineer</h1><a href="/companies/company">Company</a>
    <div class="job-description">Python</div>
    <a href="javascript:alert(1)">Apply now</a>
    <a href="https://jobs.lever.co/company/123">Apply</a></main>"""
    result = ADAPTERS["realwork"].metadata(listing, html)
    assert result.company == "Company"
    assert result.title == "Backend engineer"
    assert result.apply_url == "https://jobs.lever.co/company/123"
    assert result.posted_at == listing.posted_at


def test_company_link_rules_reject_spoofed_hosts_and_paths():
    for adapter in ("40seas", "ai21", "audiocodes", "akeyless"):
        with pytest.raises(ValueError):
            ADAPTERS[adapter].discover(
                b'<a href="https://comeet.com.evil.example/jobs/x">Bad</a>'
                b'<a href="https://example.com/about">About</a>',
                "https://example.com/careers",
                10,
            )


def test_comeet_embedded_json_metadata_without_executing_scripts():
    data = b"""<h1>Engineer</h1><div class="userDesignedContent company-description"></div>
    <script>var POSITION_DATA = {"name":"Backend Engineer", "location":{"name":"Tel Aviv"},
    "custom_fields":{"details":[{"name":"Requirements","value":"<p>Python</p>"}]}};
    doNotExecute();</script>"""
    result = ADAPTERS["akeyless"].metadata(
        Listing("https://www.comeet.com/jobs/x", "Engineer"), data
    )
    assert result.title == "Backend Engineer" and result.description == "Python"
    assert result.location == "Tel Aviv"
