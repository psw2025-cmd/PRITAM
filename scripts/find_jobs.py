#!/usr/bin/env python3
"""Search current piping vacancies and public role-based recruiting emails."""

from __future__ import annotations

import argparse
import csv
import html
import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus, urlparse
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

UA = "Mozilla/5.0 (compatible; Pritam-Piping-Job-Finder/1.0; +https://psw2025-cmd.github.io/PRITAM/)"
EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,24}\b")
ROLE_EMAIL_RE = re.compile(
    r"(?i)(career|careers|job|jobs|hr|recruit|recruitment|recruiting|talent|"
    r"hiring|resume|resumes|cv|employment|resourcing|staffing|manpower)"
)
BLOCKED_EMAIL_RE = re.compile(
    r"(?i)(no-?reply|donotreply|privacy|legal|security|abuse|webmaster|"
    r"support|helpdesk|customer|sales|marketing|press|media|dpo)"
)
RELEVANCE = ("piping", "pipe design", "e3d", "pdms", "sp3d", "smartplant 3d", "piping layout")
JOB_WORDS = ("job", "career", "vacancy", "opening", "recruit", "engineer", "designer", "checker")
NEGATIVE = ("course", "training", "certification", "salary guide", "interview questions")


@dataclass
class Lead:
    score: int
    title: str
    company: str
    location: str
    published_date: str
    valid_through: str
    source_type: str
    source_domain: str
    public_recruiting_emails: list[str]
    url: str
    search_query: str
    snippet: str


def fetch(url: str, timeout: int = 20, limit: int = 2_000_000) -> str:
    req = Request(url, headers={"User-Agent": UA, "Accept": "text/html,application/xml;q=0.9,*/*;q=0.7"})
    with urlopen(req, timeout=timeout) as response:
        data = response.read(limit + 1)[:limit]
        return data.decode(response.headers.get_content_charset() or "utf-8", errors="replace")


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", value or ""))).strip()


def parse_date(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        try:
            return parsedate_to_datetime(value).date().isoformat()
        except (TypeError, ValueError, OverflowError):
            return ""


def domain(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def relevant(text: str) -> bool:
    text = text.lower()
    return (
        not any(term in text for term in NEGATIVE)
        and any(term in text for term in RELEVANCE)
        and any(term in text for term in JOB_WORDS)
    )


def rss_items(query: str) -> list[dict[str, str]]:
    root = ET.fromstring(fetch("https://www.bing.com/search?format=rss&q=" + quote_plus(query)))
    rows = []
    for item in root.findall(".//item"):
        url = clean(item.findtext("link", ""))
        if url:
            rows.append(
                {
                    "title": clean(item.findtext("title", "")),
                    "url": url,
                    "snippet": clean(item.findtext("description", "")),
                    "published_date": parse_date(item.findtext("pubDate", "")),
                }
            )
    return rows


def walk_json(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_json(child)


def job_jsonld(page: str) -> dict[str, Any]:
    pattern = r"(?is)<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>"
    for raw in re.findall(pattern, page):
        try:
            payload = json.loads(html.unescape(raw).strip())
        except (json.JSONDecodeError, TypeError):
            continue
        for obj in walk_json(payload):
            types = obj.get("@type", [])
            if not isinstance(types, list):
                types = [types]
            if any(str(x).lower() == "jobposting" for x in types):
                return obj
    return {}


def jsonld_company(obj: dict[str, Any]) -> str:
    org = obj.get("hiringOrganization", {})
    return clean(str(org.get("name", ""))) if isinstance(org, dict) else ""


def jsonld_location(obj: dict[str, Any]) -> str:
    locations = obj.get("jobLocation", [])
    if isinstance(locations, dict):
        locations = [locations]
    found = []
    if isinstance(locations, list):
        for item in locations:
            if not isinstance(item, dict):
                continue
            address = item.get("address", {})
            if isinstance(address, str):
                found.append(address)
            elif isinstance(address, dict):
                parts = [address.get("addressLocality"), address.get("addressRegion"), address.get("addressCountry")]
                value = ", ".join(str(x) for x in parts if x)
                if value:
                    found.append(value)
    if "telecommute" in str(obj.get("jobLocationType", "")).lower():
        found.append("Remote")
    return " / ".join(dict.fromkeys(found))


def public_job_emails(page: str, own_email: str) -> list[str]:
    found = []
    for value in sorted(set(EMAIL_RE.findall(html.unescape(page)))):
        value = value.lower().strip(".,;:()[]{}<>")
        local, _, host = value.partition("@")
        if value == own_email.lower() or not local or not host:
            continue
        if host.endswith((".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js")):
            continue
        if BLOCKED_EMAIL_RE.search(local) or not ROLE_EMAIL_RE.search(local):
            continue
        found.append(value)
    return found[:10]


def company_source(host: str, official: dict[str, str]) -> tuple[str, str]:
    for official_host, name in official.items():
        if host == official_host or host.endswith("." + official_host):
            return name, "official_career_site"
    boards = ("linkedin.com", "naukri.com", "indeed.com", "foundit.in", "bayt.com",
              "gulftalent.com", "energyjobline.com", "rigzone.com", "careerjet")
    return ("", "job_board") if any(x in host for x in boards) else ("", "other_public_source")


def infer_location(text: str, configured: list[str]) -> str:
    options = configured + [
        "UAE", "Dubai", "Abu Dhabi", "Saudi Arabia", "Riyadh", "Qatar", "Doha",
        "Oman", "Muscat", "Kuwait", "Bahrain", "Pune", "Chennai", "Bengaluru",
        "Bangalore", "Hyderabad", "Vadodara", "Gurugram", "Gurgaon", "Noida",
    ]
    lower = text.lower()
    return ", ".join(dict.fromkeys(x for x in options if x.lower() in lower))[:200]


def build_queries(config: dict[str, Any]) -> list[str]:
    locations = " OR ".join(f'"{x}"' for x in config["search_locations"])
    queries = []
    for group in config["role_query_groups"]:
        roles = " OR ".join(f'"{x}"' for x in group)
        queries.append(f"({roles}) ({locations}) (job OR vacancy OR careers)")
    for host in config["official_company_domains"]:
        queries.append(f"site:{host} (piping OR E3D OR PDMS OR SP3D) (engineer OR designer OR checker)")
    queries.extend(config.get("extra_queries", []))
    return list(dict.fromkeys(queries))


def age_days(value: str) -> int | None:
    try:
        return (date.today() - date.fromisoformat(value)).days if value else None
    except ValueError:
        return None


def rank(lead: Lead, roles: list[str], locations: list[str]) -> int:
    text = f"{lead.title} {lead.snippet}".lower()
    score = 35 if lead.source_type == "official_career_site" else 15 if lead.source_type == "job_board" else 5
    score += 25 if any(role.lower() in text for role in roles) else 15
    score += 15 if any(x.lower() in lead.location.lower() for x in locations) else 0
    score += 15 if lead.public_recruiting_emails else 0
    age = age_days(lead.published_date)
    score += 20 if age is not None and 0 <= age <= 30 else 10 if age is not None and age <= 90 else 0
    if lead.valid_through:
        try:
            if date.fromisoformat(lead.valid_through) < date.today():
                score -= 100
        except ValueError:
            pass
    return score


def write_results(output: Path, report_path: Path, leads: list[Lead], queries: list[str], failures: list[dict]):
    output.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    generated = datetime.now(timezone.utc).isoformat()
    jobs = [asdict(x) for x in leads]
    payload = {
        "generated_at_utc": generated,
        "job_count": len(jobs),
        "jobs_with_public_recruiting_email": sum(bool(x.public_recruiting_emails) for x in leads),
        "jobs": jobs,
    }
    (output / "latest_jobs.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    fields = list(asdict(leads[0]).keys()) if leads else [
        "score", "title", "company", "location", "published_date", "valid_through",
        "source_type", "source_domain", "public_recruiting_emails", "url", "search_query", "snippet",
    ]
    with (output / "latest_jobs.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for lead in leads:
            row = asdict(lead)
            row["public_recruiting_emails"] = "; ".join(lead.public_recruiting_emails)
            writer.writerow(row)

    email_fields = ["email", "company", "job_title", "location", "job_url", "source_domain"]
    with (output / "latest_job_emails.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=email_fields)
        writer.writeheader()
        seen = set()
        for lead in leads:
            for email in lead.public_recruiting_emails:
                key = (email, lead.url)
                if key in seen:
                    continue
                seen.add(key)
                writer.writerow({
                    "email": email, "company": lead.company, "job_title": lead.title,
                    "location": lead.location, "job_url": lead.url, "source_domain": lead.source_domain,
                })

    lines = [
        "# Latest piping job leads", "", f"Generated: `{generated}`", "",
        f"- Relevant leads: **{len(leads)}**",
        f"- Leads with public role-based recruiting email: **{payload['jobs_with_public_recruiting_email']}**",
        f"- Search queries attempted: **{len(queries)}**",
        f"- Query failures: **{len(failures)}**", "",
        "> Confirm the vacancy is still open on the linked page before applying. Only publicly displayed role-based recruiting addresses are recorded.", "",
        "| Score | Role | Company | Location | Posted | Source | Public job email |",
        "|---:|---|---|---|---|---|---|",
    ]
    for lead in leads[:75]:
        source = f"[{lead.source_domain}]({lead.url})"
        lines.append(
            f"| {lead.score} | {lead.title.replace('|','/')} | {(lead.company or 'Not stated').replace('|','/')} | "
            f"{(lead.location or 'Not stated').replace('|','/')} | {lead.published_date or 'Unverified'} | "
            f"{source} | {', '.join(lead.public_recruiting_emails) or '—'} |"
        )
    (output / "latest_jobs.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    report = {
        "status": "PASS" if len(failures) < len(queries) else "FAIL",
        "generated_at_utc": generated,
        "queries_attempted": len(queries),
        "queries_succeeded": len(queries) - len(failures),
        "query_failures": failures,
        "relevant_leads": len(leads),
        "official_career_site_leads": sum(x.source_type == "official_career_site" for x in leads),
        "job_board_leads": sum(x.source_type == "job_board" for x in leads),
        "leads_with_public_recruiting_email": payload["jobs_with_public_recruiting_email"],
        "outputs": [str(output / x) for x in (
            "latest_jobs.json", "latest_jobs.csv", "latest_job_emails.csv", "latest_jobs.md"
        )],
    }
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="job_search_config.json")
    ap.add_argument("--profile", default="profile.json")
    ap.add_argument("--output-dir", default="job-results")
    ap.add_argument("--report", default="proof/job-search-report.json")
    ap.add_argument("--max-results", type=int, default=100)
    ap.add_argument("--delay", type=float, default=0.7)
    args = ap.parse_args()

    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    profile = json.loads(Path(args.profile).read_text(encoding="utf-8"))
    official = {k.lower(): v for k, v in config["official_company_domains"].items()}
    queries = build_queries(config)
    failures, leads = [], {}

    for number, query in enumerate(queries, 1):
        print(f"[{number}/{len(queries)}] {query}", flush=True)
        try:
            items = rss_items(query)
        except (HTTPError, URLError, TimeoutError, ET.ParseError, OSError) as exc:
            failures.append({"query": query, "error": f"{type(exc).__name__}: {exc}"})
            print(f"WARNING: {exc}", file=sys.stderr)
            continue
        for item in items:
            combined = f"{item['title']} {item['snippet']} {item['url']}"
            if not relevant(combined):
                continue
            host = domain(item["url"])
            company, source = company_source(host, official)
            title, location = item["title"], infer_location(combined, config["search_locations"])
            posted, valid, emails = item["published_date"], "", []
            try:
                page = fetch(item["url"])
                obj = job_jsonld(page)
                if obj:
                    title = clean(str(obj.get("title", ""))) or title
                    company = jsonld_company(obj) or company
                    location = jsonld_location(obj) or location
                    posted = parse_date(str(obj.get("datePosted", ""))) or posted
                    valid = parse_date(str(obj.get("validThrough", "")))
                emails = public_job_emails(page, profile["email"])
            except (HTTPError, URLError, TimeoutError, OSError, ValueError):
                pass
            lead = Lead(
                0, title[:300], company[:150], location[:200], posted, valid, source, host,
                emails, item["url"], query, item["snippet"][:600],
            )
            lead.score = rank(lead, profile.get("target_roles", []), profile.get("preferred_locations", []))
            if lead.url not in leads or lead.score > leads[lead.url].score:
                leads[lead.url] = lead
        time.sleep(args.delay)

    ordered = sorted(leads.values(), key=lambda x: (x.score, x.published_date, x.title.lower()), reverse=True)
    ordered = [x for x in ordered if x.score > -50][:args.max_results]
    write_results(Path(args.output_dir), Path(args.report), ordered, queries, failures)
    summary = {
        "status": "PASS" if len(failures) < len(queries) else "FAIL",
        "queries": len(queries), "query_failures": len(failures), "jobs": len(ordered),
        "jobs_with_public_recruiting_email": sum(bool(x.public_recruiting_emails) for x in ordered),
    }
    print(json.dumps(summary, indent=2))
    return 1 if summary["status"] == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
