"""The OpenRouter balance probe: where the key comes from, what the two ceilings
read, and what happens when the account cannot be reached.

The endpoints are stubbed at :func:`spend._get`, so nothing here touches the
network. What the real ones return is pinned in one place — :data:`KEY_BODY` and
:data:`CREDITS_BODY` are the shapes ``/api/v1/key`` and ``/api/v1/credits`` actually
answered with, fields and all, so a test that passes against a made-up envelope
cannot pass here.
"""

from __future__ import annotations

import pytest

from diplomat_runtime import spend

#: One live reading of each endpoint, trimmed of nothing that matters. `limit` is the
#: cap set on the key, `limit_remaining` what is left of it; `total_credits` is what
#: was bought and `total_usage` what has been spent against it, account-wide.
KEY_BODY = {
    "label": "sk-or-v1-08a...c5a", "limit": 25, "limit_reset": "weekly",
    "limit_remaining": 16.850165249, "usage": 8.149834751, "is_free_tier": False,
}
CREDITS_BODY = {"total_credits": 255, "total_usage": 237.97353712}


@pytest.fixture(autouse=True)
def _probe_on(monkeypatch, tmp_path):
    """The conftest switches the probe off for every other test; these are the ones
    that mean to exercise it, with a key on disk where Hermes keeps one."""
    monkeypatch.setenv("DIPLOMAT_SPEND_PROBE", "1")
    env = tmp_path / "hermes" / ".env"
    env.parent.mkdir(parents=True, exist_ok=True)
    env.write_text("OPENROUTER_API_KEY=sk-or-v1-test\n", encoding="utf-8")
    monkeypatch.setenv("DIPLOMAT_HERMES_ENV", str(env))
    spend._reset_cache()
    yield


def _endpoints(monkeypatch, key=KEY_BODY, credits=CREDITS_BODY):
    """Stub both GETs, and record which URLs were asked for."""
    asked: list[str] = []

    def get(url, api_key):
        asked.append(url)
        return key if url == spend._KEY_URL else credits

    monkeypatch.setattr(spend, "_get", get)
    return asked


# MARK: - The key


def test_the_key_is_read_from_the_runners_own_env_file(monkeypatch, tmp_path):
    """Diplomat's config file holds the CHOICE of runner and model; the secret lives
    in the runner's own store, which is where its login wizard writes it."""
    assert spend.api_key() == "sk-or-v1-test"

    # Quoted, commented and blank lines are all ordinary in an env file.
    spend.env_path().write_text(
        '# provider keys\n\nOPENROUTER_API_KEY="sk-or-v1-quoted"\n'
        "HERMES_TUI_DIR=/opt/hermes\n", encoding="utf-8")
    assert spend.api_key() == "sk-or-v1-quoted"


def test_a_missing_file_falls_back_to_the_environment(monkeypatch):
    spend.env_path().unlink()
    assert spend.api_key() is None

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-from-env")
    assert spend.api_key() == "sk-or-v1-from-env"


def test_the_file_wins_over_a_stale_export(monkeypatch):
    """The file is the one the AGENT will be billed through. A key exported into the
    applet's own shell would otherwise price a task against an account no run
    touches."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-stale")
    assert spend.api_key() == "sk-or-v1-test"


def test_no_key_is_no_reading_rather_than_a_zero_balance(monkeypatch):
    """A machine with no OpenRouter account must not look like one that has run out
    of money — that is the difference between no opinion and a refusal."""
    spend.env_path().unlink()
    called = _endpoints(monkeypatch)

    assert spend.balance() == spend.Balance()
    assert called == [], "no key, no request"


# MARK: - The two ceilings


def test_both_ceilings_are_read_from_the_live_shapes(monkeypatch):
    _endpoints(monkeypatch)

    balance = spend.balance()

    assert balance.key_left == pytest.approx(16.850165249)
    # What was bought, less what has been spent against it: 255 - 237.97…
    assert balance.credit_left == pytest.approx(17.02646288)
    assert balance.known


def test_an_uncapped_key_leaves_the_balance_to_gate(monkeypatch):
    """``limit`` is null for a key with no cap of its own, and so is
    ``limit_remaining``. That ceiling does not exist rather than sitting at zero."""
    _endpoints(monkeypatch, key={"limit": None, "limit_remaining": None, "usage": 3.0})

    balance = spend.balance()

    assert balance.key_left is None
    assert balance.credit_left == pytest.approx(17.02646288)
    assert balance.known, "one ceiling is still a reading"


def test_a_spent_account_reads_zero_rather_than_negative(monkeypatch):
    """Usage can overshoot the credits bought on a burst. A negative balance would
    price a task as costing less than nothing on the way through the gate."""
    _endpoints(monkeypatch, credits={"total_credits": 10, "total_usage": 12.5})

    assert spend.balance().credit_left == 0.0


def test_a_body_that_is_not_a_reading_is_not_a_reading(monkeypatch):
    """A field that is missing, null, a string or a bool is no answer for that
    ceiling — never a zero, which the gate would read as "spend nothing more"."""
    _endpoints(monkeypatch, key={"limit_remaining": "16.85"},
               credits={"total_credits": True, "total_usage": 1})

    balance = spend.balance()

    assert balance.key_left is None
    assert balance.credit_left is None
    assert not balance.known


# MARK: - Failing


def test_an_unreachable_account_reports_nothing_and_never_raises(monkeypatch):
    """Both endpoints down is the fail-open the gate is built around: no reading, no
    opinion, work proceeds."""
    monkeypatch.setattr(spend, "_get", lambda url, key: None)

    assert spend.balance() == spend.Balance()


def test_one_endpoint_down_still_reports_the_other(monkeypatch):
    _endpoints(monkeypatch, key=None)

    balance = spend.balance()

    assert balance.key_left is None
    assert balance.credit_left == pytest.approx(17.02646288)


def test_the_switch_off_is_honoured_before_anything_is_read(monkeypatch):
    monkeypatch.setenv("DIPLOMAT_SPEND_PROBE", "0")
    called = _endpoints(monkeypatch)

    assert spend.balance() == spend.Balance()
    assert called == []


# MARK: - Caching


def test_one_reading_serves_a_poll_that_asks_repeatedly(monkeypatch):
    """The gate asks per dispatch and a poll can find eight units of owed work; the
    account's balance cannot move between two of them."""
    called = _endpoints(monkeypatch)

    for _ in range(8):
        spend.balance()

    assert called == [spend._KEY_URL, spend._CREDITS_URL]


def test_a_reading_stale_beyond_trust_is_dropped(monkeypatch):
    """Dollars are spent by every agent on the machine, not just by Diplomat's. A
    balance kept past the keep window would wave through work the account can no
    longer pay for."""
    _endpoints(monkeypatch)
    assert spend.balance().known

    # The account goes unreachable, and the reading ages past what it is trusted
    # for — aged by rewinding the cache rather than by faking the process clock,
    # which every other module reads too.
    monkeypatch.setattr(spend, "_get", lambda url, key: None)
    spend._cache["attempt"] -= spend._TTL_SECS + 1
    spend._cache["good"] -= spend._KEEP_SECS + 1

    assert spend.balance() == spend.Balance()
