"""Cloud Bill Watchdog.

Weekly Lambda: pulls AWS spend from Cost Explorer, compares this week to last
week by service, has Bedrock write a plain English summary, and emails it
through SNS. If Bedrock fails for any reason, the email still goes out with the
raw numbers.

Environment variables:
  TOPIC_ARN     ARN of the SNS topic to publish to
  MODEL_ID      Bedrock model or inference profile ID
  WEEKLY_LIMIT  Optional. Dollar amount that triggers an ALERT subject line. Default 5.
"""

import json
import logging
import os
from collections import defaultdict
from datetime import date, timedelta

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Cost Explorer is a global service served from us-east-1.
CE_REGION = "us-east-1"
# SNS rejects email subjects longer than 100 characters.
MAX_SUBJECT = 100

SYSTEM_PROMPT = (
    "You are a frugal AWS cost analyst writing a weekly email to the account owner.\n"
    "Using the JSON report you are given, write a summary under 200 words:\n"
    "1. One line verdict: all quiet, worth a look, or act now.\n"
    "2. Total this week vs last week and month to date.\n"
    "3. Any service that is new, or up more than 50 percent, and the likely cause.\n"
    "4. One concrete action to save money or avoid surprise charges.\n"
    "If spend is near zero, say so briefly. Use only the numbers provided. "
    "Plain text only. Do not use any dash or hyphen characters."
)


def date_windows(today):
    """Return the date boundaries the report needs.

    Cost Explorer treats End as exclusive, so "this week" is the 7 full days
    before today and "last week" is the 7 days before those.
    """
    return {
        "last_week_start": today - timedelta(days=14),
        "this_week_start": today - timedelta(days=7),
        "month_start": today.replace(day=1),
        "end": today,
    }


def fetch_daily_costs(ce, start, end):
    """Return {iso_date: {service: amount}} for [start, end).

    One DAILY query grouped by SERVICE covers every window, so a run costs a
    single Cost Explorer request (plus any extra pages). Credits and refunds
    are excluded so promotional credits can't hide real usage.
    """
    params = {
        "TimePeriod": {"Start": start.isoformat(), "End": end.isoformat()},
        "Granularity": "DAILY",
        "Metrics": ["UnblendedCost"],
        "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}],
        "Filter": {"Not": {"Dimensions": {"Key": "RECORD_TYPE", "Values": ["Credit", "Refund"]}}},
    }
    daily = defaultdict(dict)
    while True:
        resp = ce.get_cost_and_usage(**params)
        for result in resp.get("ResultsByTime", []):
            day = result["TimePeriod"]["Start"]
            for group in result.get("Groups", []):
                service = group["Keys"][0]
                amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
                daily[day][service] = daily[day].get(service, 0.0) + amount
        token = resp.get("NextPageToken")
        if not token:
            return dict(daily)
        params["NextPageToken"] = token


def sum_window(daily, start, end):
    """Total cost per service for days in [start, end), dropping services that round to zero."""
    totals = defaultdict(float)
    lo, hi = start.isoformat(), end.isoformat()
    for day, services in daily.items():
        if lo <= day < hi:
            for service, amount in services.items():
                totals[service] += amount
    return {k: round(v, 2) for k, v in totals.items() if round(v, 2) != 0}


def build_report(this_week, last_week, month_to_date, weekly_limit, period_end):
    """Compare two weeks by service and return a JSON friendly report."""
    rows = []
    for s in sorted(set(this_week) | set(last_week)):
        now, before = this_week.get(s, 0.0), last_week.get(s, 0.0)
        rows.append({
            "service": s,
            "this_week": now,
            "last_week": before,
            "change": round(now - before, 2),
            "new_this_week": before == 0 and now > 0,
        })
    rows.sort(key=lambda r: r["this_week"], reverse=True)

    total_this = round(sum(this_week.values()), 2)
    return {
        "period_end": period_end,
        "total_this_week": total_this,
        "total_last_week": round(sum(last_week.values()), 2),
        "month_to_date": round(sum(month_to_date.values()), 2),
        "weekly_limit": weekly_limit,
        "over_limit": total_this > weekly_limit,
        "services": rows,
    }


def format_raw(report, top_n=10):
    """Render the report as a fixed width plain text table."""
    change = report["total_this_week"] - report["total_last_week"]
    lines = [
        f"Week ending {report['period_end']} (end exclusive)",
        f"This week: ${report['total_this_week']:.2f}   Last week: ${report['total_last_week']:.2f}"
        f"   Change: ${change:+.2f}",
        f"Month to date: ${report['month_to_date']:.2f}   Weekly limit: ${report['weekly_limit']:.2f}"
        + ("   OVER LIMIT" if report["over_limit"] else ""),
        "",
        f"{'Service':<44}{'Last':>10}{'This':>10}{'Change':>10}",
    ]
    for r in report["services"][:top_n]:
        tag = "  NEW" if r["new_this_week"] else ""
        lines.append(
            f"{r['service'][:43]:<44}{r['last_week']:>10.2f}{r['this_week']:>10.2f}{r['change']:>+10.2f}{tag}"
        )
    hidden = len(report["services"]) - top_n
    if hidden > 0:
        lines.append(f"... and {hidden} more services")
    if not report["services"]:
        lines.append("(no spend recorded)")
    return "\n".join(lines)


def summarize(bedrock, model_id, report):
    """Ask Bedrock for a summary. Raises on any failure so the caller can fall back."""
    resp = bedrock.converse(
        modelId=model_id,
        system=[{"text": SYSTEM_PROMPT}],
        messages=[{"role": "user", "content": [{"text": json.dumps(report, indent=2)}]}],
        inferenceConfig={"maxTokens": 600},
    )
    parts = resp["output"]["message"]["content"]
    text = "".join(p.get("text", "") for p in parts).strip()
    if not text:
        raise ValueError("Bedrock returned an empty summary")
    return text


def build_message(report, summary, error=None):
    """Return (subject, body) for SNS. A missing summary means the fallback email."""
    change = report["total_this_week"] - report["total_last_week"]
    subject = (
        f"{'ALERT' if report['over_limit'] else 'Weekly'} AWS spend: "
        f"${report['total_this_week']:.2f} this week ({change:+.2f} vs last week)"
    )[:MAX_SUBJECT]

    if summary:
        body = f"{summary}\n\nRAW NUMBERS\n\n{format_raw(report)}"
    else:
        reason = f" ({error})" if error else ""
        body = f"AI summary unavailable{reason}. Raw numbers below.\n\n{format_raw(report)}"
    return subject, body


def run(ce, bedrock, sns, topic_arn, model_id, weekly_limit, today):
    """Do one full watchdog pass with injected clients. Returns the report."""
    w = date_windows(today)
    daily = fetch_daily_costs(ce, min(w["last_week_start"], w["month_start"]), w["end"])
    report = build_report(
        this_week=sum_window(daily, w["this_week_start"], w["end"]),
        last_week=sum_window(daily, w["last_week_start"], w["this_week_start"]),
        month_to_date=sum_window(daily, w["month_start"], w["end"]),
        weekly_limit=weekly_limit,
        period_end=w["end"].isoformat(),
    )

    summary, error = None, None
    try:
        summary = summarize(bedrock, model_id, report)
    except Exception as exc:  # noqa: BLE001  still send the numbers if Bedrock fails
        error = type(exc).__name__
        logger.exception("Bedrock summary failed, sending raw numbers only")

    subject, body = build_message(report, summary, error)
    sns.publish(TopicArn=topic_arn, Subject=subject, Message=body)
    logger.info(json.dumps({"report": report, "ai_summary": summary is not None}))
    return report


def lambda_handler(event, context):
    report = run(
        ce=boto3.client("ce", region_name=CE_REGION),
        bedrock=boto3.client("bedrock-runtime"),
        sns=boto3.client("sns"),
        topic_arn=os.environ["TOPIC_ARN"],
        model_id=os.environ["MODEL_ID"],
        weekly_limit=float(os.environ.get("WEEKLY_LIMIT", "5")),
        today=date.today(),
    )
    return {
        "sent": True,
        "over_limit": report["over_limit"],
        "total_this_week": report["total_this_week"],
    }
