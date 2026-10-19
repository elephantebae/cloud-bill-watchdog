"""
Cloud Bill Watchdog
Weekly Lambda: pulls AWS spend from Cost Explorer, compares this week to last week,
has Bedrock write a plain English summary, and emails it through SNS.

Environment variables:
  TOPIC_ARN     ARN of the SNS topic to publish to
  MODEL_ID      Bedrock model or inference profile ID (copy it from the Bedrock console)
  WEEKLY_LIMIT  Optional. Dollar amount that triggers an ALERT subject line. Default 5.
"""

import datetime as dt
import json
import os

import boto3

# Cost Explorer is a global service served from us-east-1
ce = boto3.client("ce", region_name="us-east-1")
sns = boto3.client("sns")
bedrock = boto3.client("bedrock-runtime")

TOPIC_ARN = os.environ["TOPIC_ARN"]
MODEL_ID = os.environ["MODEL_ID"]
WEEKLY_LIMIT = float(os.environ.get("WEEKLY_LIMIT", "5"))


def spend_by_service(start, end):
    """Total UnblendedCost per service between start (inclusive) and end (exclusive)."""
    totals = {}
    token = None
    while True:
        kwargs = {
            "TimePeriod": {"Start": start.isoformat(), "End": end.isoformat()},
            "Granularity": "DAILY",
            "Metrics": ["UnblendedCost"],
            "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}],
        }
        if token:
            kwargs["NextPageToken"] = token
        resp = ce.get_cost_and_usage(**kwargs)
        for day in resp["ResultsByTime"]:
            for group in day["Groups"]:
                service = group["Keys"][0]
                amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
                totals[service] = totals.get(service, 0.0) + amount
        token = resp.get("NextPageToken")
        if not token:
            break
    return {k: round(v, 2) for k, v in totals.items() if round(v, 2) != 0}


def build_report():
    today = dt.date.today()
    week = dt.timedelta(days=7)
    this_week = spend_by_service(today - week, today)
    last_week = spend_by_service(today - 2 * week, today - week)

    month_start = today.replace(day=1)
    month_to_date = spend_by_service(month_start, today) if month_start < today else {}

    services = sorted(set(this_week) | set(last_week))
    rows = []
    for s in services:
        now, before = this_week.get(s, 0.0), last_week.get(s, 0.0)
        rows.append({
            "service": s,
            "this_week": now,
            "last_week": before,
            "change": round(now - before, 2),
            "new_this_week": before == 0 and now > 0,
        })
    rows.sort(key=lambda r: r["this_week"], reverse=True)

    return {
        "period_end": today.isoformat(),
        "total_this_week": round(sum(this_week.values()), 2),
        "total_last_week": round(sum(last_week.values()), 2),
        "month_to_date": round(sum(month_to_date.values()), 2),
        "weekly_limit": WEEKLY_LIMIT,
        "services": rows,
    }


def summarize(report):
    prompt = (
        "You are a frugal AWS cost analyst writing a weekly email to the account owner.\n"
        "Using the JSON below, write a summary under 200 words:\n"
        "1. One line verdict: all quiet, worth a look, or act now.\n"
        "2. Total this week vs last week and month to date.\n"
        "3. Any service that is new, or up more than 50 percent, and the likely cause.\n"
        "4. One concrete action to save money or avoid surprise charges.\n"
        "If spend is near zero, say so briefly. Plain text only. "
        "Do not use any dash or hyphen characters.\n\n"
        + json.dumps(report, indent=2)
    )
    resp = bedrock.converse(
        modelId=MODEL_ID,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": 600},
    )
    return resp["output"]["message"]["content"][0]["text"]


def lambda_handler(event, context):
    report = build_report()

    try:
        summary = summarize(report)
    except Exception as exc:  # Still send the numbers if Bedrock fails
        summary = f"AI summary unavailable ({exc}). Raw numbers below."

    over = report["total_this_week"] > WEEKLY_LIMIT
    subject = (
        f"{'ALERT' if over else 'Weekly'} AWS spend: "
        f"${report['total_this_week']:.2f} this week"
    )
    body = summary + "\n\n---- raw data ----\n" + json.dumps(report, indent=2)

    sns.publish(TopicArn=TOPIC_ARN, Subject=subject[:100], Message=body)
    print(json.dumps(report))
    return {"sent": True, "over_limit": over, "total_this_week": report["total_this_week"]}
