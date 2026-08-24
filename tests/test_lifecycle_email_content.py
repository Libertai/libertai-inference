"""Template rendering for lifecycle emails: subjects, greeting fallback, escaping."""

from datetime import datetime

from src.models.user import User
from src.services.lifecycle_email import render_email


def _user(display_name=None) -> User:
    return User(email="render@example.com", display_name=display_name)


def test_subjects_come_from_templates():
    # No brand in subjects: the sender name already carries it.
    assert render_email("paid_welcome", _user(), {"tier": "go"})[0] == "Your Go plan is active"
    assert render_email("payment_failed", _user(), {"tier": "plus"})[0] == "Your payment didn't go through"
    assert render_email("cancellation_confirmed", _user(), {"until": None})[0] == (
        "Your subscription has been cancelled"
    )


def test_greeting_falls_back_without_display_name():
    html = render_email("paid_welcome", _user(), {"tier": "go"})[1].replace("\n", "")
    assert "Hi,</p>" in html
    html = render_email("paid_welcome", _user("Ada"), {"tier": "go"})[1].replace("\n", "")
    assert "Hi Ada,</p>" in html


def test_display_name_is_escaped():
    html = render_email("paid_welcome", _user("<b>x</b>"), {"tier": "go"})[1]
    assert "<b>x</b>" not in html and "&lt;b&gt;x&lt;/b&gt;" in html


def test_cancellation_confirmed_includes_end_date_when_known():
    html = render_email("cancellation_confirmed", _user(), {"until": datetime(2026, 8, 15)})[1]
    assert "until August 15, 2026" in html
    html = render_email("cancellation_confirmed", _user(), {"until": None})[1]
    first_paragraph = html.split("billed")[0]
    assert "until" not in first_paragraph


def test_plan_name_capitalized_in_bodies():
    assert "the Go plan" in render_email("paid_welcome", _user(), {"tier": "go"})[1]
    assert "your Plus subscription" in render_email("payment_failed", _user(), {"tier": "plus"})[1]


def test_unsubscribe_footer_only_when_url_given():
    with_url = render_email("paid_welcome", _user(), {"tier": "go"}, unsubscribe_url="http://u/x")[1]
    assert 'href="http://u/x"' in with_url and "Unsubscribe" in with_url
    without = render_email("paid_welcome", _user(), {"tier": "go"})[1]
    assert "Unsubscribe" not in without
