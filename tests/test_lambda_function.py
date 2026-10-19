from datetime import date
from unittest.mock import MagicMock

import boto3
import pytest
from botocore.stub import ANY, Stubber

import lambda_function as lf

TODAY = date(2026, 10, 19)  # a Monday
TOPIC = "arn:aws:sns:us-west-2:000000000000:BillWatchdog"
MODEL = "us.anthropic.claude-test"


def ce_day(day, costs):
    """Build one ResultsByTime entry in the Cost Explorer response shape."""
    return {
        "TimePeriod": {"Start": day, "End": day},
        "Total": {},
        "Groups": [
            {"Keys": [svc], "Metrics": {"UnblendedCost": {"Amount": str(amt), "Unit": "USD"}}}
            for svc, amt in costs.items()
        ],
        "Estimated": False,
    }


def converse_reply(text):
    return {
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
        "stopReason": "end_turn",
        "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
        "metrics": {"latencyMs": 1},
    }


# report building

def test_date_windows():
    w = lf.date_windows(TODAY)
    assert w["last_week_start"] == date(2026, 10, 5)
    assert w["this_week_start"] == date(2026, 10, 12)
    assert w["month_start"] == date(2026, 10, 1)
    assert w["end"] == TODAY


def test_sum_window_respects_bounds_and_drops_zero():
    daily = {
        "2026-10-04": {"EC2": 9.0},
        "2026-10-05": {"EC2": 1.0, "Tiny": 0.001},
        "2026-10-11": {"EC2": 2.0, "S3": 0.5},
        "2026-10-12": {"EC2": 4.0},
    }
    assert lf.sum_window(daily, date(2026, 10, 5), date(2026, 10, 12)) == {"EC2": 3.0, "S3": 0.5}


def test_build_report_totals_flags_and_ordering():
    report = lf.build_report(
        this_week={"EC2": 5.0, "S3": 0.9, "Bedrock": 0.4},
        last_week={"EC2": 2.0, "S3": 1.0},
        month_to_date={"EC2": 12.0, "S3": 3.0},
        weekly_limit=5,
        period_end="2026-10-19",
    )
    assert report["total_this_week"] == 6.3
    assert report["total_last_week"] == 3.0
    assert report["month_to_date"] == 15.0
    assert report["over_limit"] is True
    assert [r["service"] for r in report["services"]] == ["EC2", "S3", "Bedrock"]
    by_name = {r["service"]: r for r in report["services"]}
    assert by_name["EC2"]["change"] == 3.0
    assert by_name["Bedrock"]["new_this_week"] is True
    assert by_name["S3"]["new_this_week"] is False


def test_format_raw_marks_new_services_and_empty_account():
    report = lf.build_report({"Bedrock": 1.0}, {}, {}, 5, "2026-10-19")
    assert "NEW" in lf.format_raw(report)
    empty = lf.build_report({}, {}, {}, 5, "2026-10-19")
    assert "(no spend recorded)" in lf.format_raw(empty)


def test_fetch_daily_costs_follows_pagination():
    ce = boto3.client("ce", region_name="us-east-1")
    with Stubber(ce) as stub:
        stub.add_response(
            "get_cost_and_usage",
            {"ResultsByTime": [ce_day("2026-10-05", {"EC2": 1.5})], "NextPageToken": "p2"},
            {"TimePeriod": {"Start": "2026-10-05", "End": "2026-10-19"},
             "Granularity": "DAILY", "Metrics": ["UnblendedCost"],
             "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}],
             "Filter": {"Not": {"Dimensions": {"Key": "RECORD_TYPE",
                                               "Values": ["Credit", "Refund"]}}}},
        )
        stub.add_response(
            "get_cost_and_usage",
            {"ResultsByTime": [ce_day("2026-10-12", {"EC2": 2.0, "S3": 0.1})]},
            {"TimePeriod": ANY, "Granularity": ANY, "Metrics": ANY, "GroupBy": ANY,
             "Filter": ANY, "NextPageToken": "p2"},
        )
        daily = lf.fetch_daily_costs(ce, date(2026, 10, 5), TODAY)
        stub.assert_no_pending_responses()
    assert daily == {"2026-10-05": {"EC2": 1.5}, "2026-10-12": {"EC2": 2.0, "S3": 0.1}}


# summary and fallback

SAMPLE = lf.build_report({"EC2": 2.0}, {"EC2": 1.0}, {"EC2": 3.0}, 5, "2026-10-19")


def test_summarize_returns_model_text():
    bedrock = boto3.client("bedrock-runtime", region_name="us-west-2")
    with Stubber(bedrock) as stub:
        stub.add_response(
            "converse", converse_reply("  Spend doubled.  "),
            {"modelId": MODEL, "system": ANY, "messages": ANY, "inferenceConfig": ANY},
        )
        assert lf.summarize(bedrock, MODEL, SAMPLE) == "Spend doubled."


def test_summarize_raises_on_empty_text():
    bedrock = MagicMock()
    bedrock.converse.return_value = {"output": {"message": {"content": [{"text": "   "}]}}}
    with pytest.raises(ValueError):
        lf.summarize(bedrock, MODEL, SAMPLE)


def test_build_message_with_and_without_summary():
    subject, body = lf.build_message(SAMPLE, "All quiet.")
    assert subject == "Weekly AWS spend: $2.00 this week (+1.00 vs last week)"
    assert body.startswith("All quiet.") and "RAW NUMBERS" in body

    over = {**SAMPLE, "over_limit": True}
    subject, body = lf.build_message(over, None, "AccessDeniedException")
    assert subject.startswith("ALERT AWS spend:")
    assert len(subject) <= lf.MAX_SUBJECT
    assert body.startswith("AI summary unavailable (AccessDeniedException)")
    assert "EC2" in body and "Month to date: $3.00" in body


def _run_with_stubs(bedrock_setup):
    ce = boto3.client("ce", region_name="us-east-1")
    bedrock = boto3.client("bedrock-runtime", region_name="us-west-2")
    sns = boto3.client("sns", region_name="us-west-2")
    published = {}
    with Stubber(ce) as ce_stub, Stubber(bedrock) as br_stub, Stubber(sns) as sns_stub:
        # One request starting at the earlier of month start (Oct 1) and last week (Oct 5).
        ce_stub.add_response(
            "get_cost_and_usage",
            {"ResultsByTime": [ce_day("2026-10-02", {"EC2": 0.5}),
                               ce_day("2026-10-06", {"EC2": 1.0}),
                               ce_day("2026-10-13", {"EC2": 3.0})]},
            {"TimePeriod": {"Start": "2026-10-01", "End": "2026-10-19"},
             "Granularity": ANY, "Metrics": ANY, "GroupBy": ANY, "Filter": ANY},
        )
        bedrock_setup(br_stub)
        sns_stub.add_response(
            "publish", {"MessageId": "1"},
            {"TopicArn": TOPIC, "Subject": ANY, "Message": ANY},
        )
        original = sns.publish

        def spy(**kwargs):
            published.update(kwargs)
            return original(**kwargs)

        sns.publish = spy
        report = lf.run(ce, bedrock, sns, TOPIC, MODEL, 5.0, TODAY)
        sns_stub.assert_no_pending_responses()
    return report, published


def test_run_happy_path_uses_one_cost_explorer_call():
    report, published = _run_with_stubs(
        lambda stub: stub.add_response("converse", converse_reply("All quiet."))
    )
    assert report["total_this_week"] == 3.0
    assert report["total_last_week"] == 1.0
    assert report["month_to_date"] == 4.5
    assert published["Message"].startswith("All quiet.")


def test_run_still_publishes_raw_numbers_when_bedrock_fails():
    report, published = _run_with_stubs(
        lambda stub: stub.add_client_error("converse", service_error_code="ThrottlingException")
    )
    assert report["total_this_week"] == 3.0
    assert published["Message"].startswith("AI summary unavailable (ThrottlingException)")
    assert "EC2" in published["Message"]


def test_lambda_handler_reads_env(monkeypatch):
    monkeypatch.setenv("TOPIC_ARN", TOPIC)
    monkeypatch.setenv("MODEL_ID", MODEL)
    monkeypatch.setenv("WEEKLY_LIMIT", "7.5")
    captured = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return {"total_this_week": 1.0, "over_limit": False}

    monkeypatch.setattr(lf, "run", fake_run)
    monkeypatch.setattr(lf.boto3, "client", MagicMock())
    result = lf.lambda_handler({}, None)
    assert captured["topic_arn"] == TOPIC
    assert captured["model_id"] == MODEL
    assert captured["weekly_limit"] == 7.5
    assert result == {"sent": True, "over_limit": False, "total_this_week": 1.0}


def test_lambda_handler_fails_loudly_without_topic(monkeypatch):
    monkeypatch.delenv("TOPIC_ARN", raising=False)
    monkeypatch.setattr(lf.boto3, "client", MagicMock())
    with pytest.raises(KeyError):
        lf.lambda_handler({}, None)
