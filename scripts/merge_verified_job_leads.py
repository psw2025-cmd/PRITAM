#!/usr/bin/env python3
"""Reject weak search hits and merge a dated register of source-verified job leads."""

from __future__ import annotations

import csv
import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

OUTPUT = Path("job-results")
LIVE_JSON = OUTPUT / "latest_jobs.json"
VERIFIED_JSON = Path("verified_job_leads.json")
REPORT_JSON = Path("proof/job-search-report.json")

ROLE_RE = re.compile(
    r"(?i)\b(piping|pipe stress|piping layout|e3d|pdms|sp3d|smartplant 3d)\b"
)
FALSE_POSITIVE_DOMAINS = {
    "aveva.com",
    "youtube.com",
    "youtu.be",
    "whatispiping.com",
    "udemy.com",
    "coursera.org",
}
JOB_BOARD_PATHS = {
    "linkedin.com": ("/jobs/view",),
    "indeed.com": ("/viewjob", "/rc/clk"),
    "naukri.com": ("/job-listings",),
    "foundit.in": ("/job/",),
    "bayt.com": ("/en/",),
    "gulftalent.com": ("/",),
}


def host(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.").removeprefix("in.").removeprefix("ae.")


def is_expired(valid_through: str) -> bool:
    if not valid_through:
        return False
    try:
        return date.fromisoformat(valid_through) < date.today()
    except ValueError:
        return True


def acceptable_live_hit(item: dict) -> tuple[bool, str]:
    title = str(item.get("title", ""))
    url = str(item.get("url", ""))
    source_type = str(item.get("source_type", ""))
    parsed = urlparse(url)
    domain = host(url)

    if not ROLE_RE.search(title):
        return False, "title lacks target piping/E3D role"
    if any(domain == bad or domain.endswith("." + bad) for bad in FALSE_POSITIVE_DOMAINS):
        return False, "known informational/training domain"
    if is_expired(str(item.get("valid_through", ""))):
        return False, "vacancy valid-through date has passed"

    if source_type == "official_career_site":
        path = parsed.path.lower()
        if not any(token in path for token in ("career", "job", "vacan", "position", "opportun")):
            return False, "official-domain result is not a career/job path"
        return True, "strict official career-page rule"

    if source_type == "job_board":
        for board, path_prefixes in JOB_BOARD_PATHS.items():
            if domain == board or domain.endswith("." + board):
                if any(prefix in parsed.path.lower() for prefix in path_prefixes):
                    return True, "strict job-board URL rule"
        return False, "job-board result does not point to a job-detail path"

    return False, "source is not an official career page or job-detail page"


def verified_to_output(item: dict) -> dict:
    source_type = item["source_type"]
    score = 110 if source_type == "official_career_site" else 100 if source_type == "job_board" else 90
    if item.get("public_recruiting_emails"):
        score += 5
    return {
        "score": score,
        "title": item["title"],
        "company": item["company"],
        "location": item["location"],
        "published_date": item.get("published_date", ""),
        "valid_through": item.get("valid_through", ""),
        "source_type": source_type,
        "source_domain": item["source_domain"],
        "public_recruiting_emails": item.get("public_recruiting_emails", []),
        "url": item["url"],
        "search_query": "source-verified register",
        "snippet": item.get("notes", ""),
        "job_id": item.get("job_id", ""),
        "status": item.get("status", "verify_before_applying"),
        "last_verified": item.get("last_verified", ""),
        "reverify_after": item.get("reverify_after", ""),
        "email_purpose": item.get("email_purpose", ""),
    }


def identity(item: dict) -> str:
    if item.get("job_id"):
        return f"{item.get('company','').lower()}|{item['job_id']}"
    return f"{item.get('company','').lower()}|{item.get('title','').lower()}|{item.get('url','').lower()}"


def write_csv(path: Path, rows: list[dict]) -> None:
    preferred = [
        "score", "title", "company", "location", "published_date", "valid_through",
        "source_type", "source_domain", "status", "last_verified", "reverify_after",
        "job_id", "public_recruiting_emails", "email_purpose", "url", "search_query", "snippet",
    ]
    fields = preferred + sorted({key for row in rows for key in row} - set(preferred))
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for source in rows:
            row = dict(source)
            emails = row.get("public_recruiting_emails", [])
            row["public_recruiting_emails"] = "; ".join(emails) if isinstance(emails, list) else emails
            writer.writerow(row)


def main() -> int:
    live_payload = json.loads(LIVE_JSON.read_text(encoding="utf-8"))
    verified_payload = json.loads(VERIFIED_JSON.read_text(encoding="utf-8"))
    original_report = json.loads(REPORT_JSON.read_text(encoding="utf-8")) if REPORT_JSON.exists() else {}

    accepted_live, rejected = [], []
    for item in live_payload.get("jobs", []):
        accepted, reason = acceptable_live_hit(item)
        if accepted:
            normalized = dict(item)
            normalized.setdefault("job_id", "")
            normalized.setdefault("status", "discovered_verify_before_applying")
            normalized.setdefault("last_verified", date.today().isoformat())
            normalized.setdefault("reverify_after", "")
            normalized.setdefault("email_purpose", "")
            accepted_live.append(normalized)
        else:
            rejected.append({"title": item.get("title", ""), "url": item.get("url", ""), "reason": reason})

    verified = []
    skipped_verified = []
    for item in verified_payload.get("leads", []):
        if is_expired(str(item.get("valid_through", ""))):
            skipped_verified.append({"title": item.get("title", ""), "reason": "expired"})
            continue
        verified.append(verified_to_output(item))

    merged: dict[str, dict] = {}
    for item in accepted_live + verified:
        key = identity(item)
        previous = merged.get(key)
        if previous is None or int(item.get("score", 0)) > int(previous.get("score", 0)):
            merged[key] = item

    jobs = sorted(
        merged.values(),
        key=lambda row: (int(row.get("score", 0)), row.get("last_verified", ""), row.get("title", "")),
        reverse=True,
    )
    generated = datetime.now(timezone.utc).isoformat()
    email_count = sum(bool(row.get("public_recruiting_emails")) for row in jobs)

    payload = {
        "generated_at_utc": generated,
        "job_count": len(jobs),
        "jobs_with_public_recruiting_email": email_count,
        "strict_live_hits": len(accepted_live),
        "source_verified_hits": len(verified),
        "rejected_weak_hits": len(rejected),
        "jobs": jobs,
    }
    LIVE_JSON.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    write_csv(OUTPUT / "latest_jobs.csv", jobs)

    email_rows = []
    seen_emails = set()
    for row in jobs:
        for email in row.get("public_recruiting_emails", []):
            key = (email.lower(), row.get("url", ""))
            if key in seen_emails:
                continue
            seen_emails.add(key)
            email_rows.append({
                "email": email,
                "company": row.get("company", ""),
                "job_title": row.get("title", ""),
                "location": row.get("location", ""),
                "status": row.get("status", ""),
                "last_verified": row.get("last_verified", ""),
                "reverify_after": row.get("reverify_after", ""),
                "email_purpose": row.get("email_purpose", ""),
                "job_url": row.get("url", ""),
                "source_domain": row.get("source_domain", ""),
            })
    write_csv(OUTPUT / "latest_job_emails.csv", email_rows)

    lines = [
        "# Latest piping job leads", "", f"Generated: `{generated}`", "",
        f"- Evidence-backed leads: **{len(jobs)}**",
        f"- Confirmed/source-verified register leads: **{len(verified)}**",
        f"- Strict live-search additions: **{len(accepted_live)}**",
        f"- Weak search hits rejected: **{len(rejected)}**",
        f"- Leads with a public application/recruiting email: **{email_count}**", "",
        "> Apply only after reopening the source. A status of `verify_before_applying` means the email was publicly posted, but the vacancy must be reconfirmed before sending personal documents.", "",
        "| Score | Role | Company | Location | Status | Verified | Source | Public job email |",
        "|---:|---|---|---|---|---|---|---|",
    ]
    for row in jobs:
        source = f"[{row.get('source_domain','source')}]({row.get('url','')})"
        emails = ", ".join(row.get("public_recruiting_emails", [])) or "—"
        values = [
            str(row.get("score", "")), row.get("title", ""), row.get("company", ""),
            row.get("location", ""), row.get("status", ""), row.get("last_verified", ""),
            source, emails,
        ]
        lines.append("| " + " | ".join(str(value).replace("|", "/") for value in values) + " |")
    (OUTPUT / "latest_jobs.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    report = dict(original_report)
    report.update({
        "status": "PASS" if jobs else "FAIL",
        "generated_at_utc": generated,
        "raw_search_leads": live_payload.get("job_count", len(live_payload.get("jobs", []))),
        "strict_live_hits": len(accepted_live),
        "source_verified_hits": len(verified),
        "final_evidence_backed_leads": len(jobs),
        "leads_with_public_recruiting_email": email_count,
        "rejected_weak_hits": rejected,
        "skipped_verified_entries": skipped_verified,
        "quality_gate": "PASS" if jobs and not any(host(row.get("url", "")) in FALSE_POSITIVE_DOMAINS for row in jobs) else "FAIL",
    })
    REPORT_JSON.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({
        "status": report["status"],
        "quality_gate": report["quality_gate"],
        "final_jobs": len(jobs),
        "public_job_emails": email_count,
        "rejected_weak_hits": len(rejected),
    }, indent=2))
    return 0 if report["status"] == "PASS" and report["quality_gate"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
