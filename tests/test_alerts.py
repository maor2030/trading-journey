from datetime import timedelta

from conftest import NOW, decision

from trading_journey import notify

CREDIT_ERROR = Exception("Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', "
                         "'message': 'Your credit balance is too low to access the Anthropic API.'}}")


def _mails(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "send_email", lambda settings, subject, body: sent.append((subject, body)) or True)
    return sent


def test_out_of_credit_emails_once_a_day(make_agent, monkeypatch):
    sent = _mails(monkeypatch)
    agent = make_agent()

    def analyze(ticker, trade_date, portfolio=None):
        raise CREDIT_ERROR

    agent.analyze_fn = analyze
    plan = agent.plan(now=NOW, tickers=["NVDA", "AMD"])
    assert plan["out_of_credit"] is True
    assert len(sent) == 1 and "NVDA, AMD" in sent[0][1]

    agent.plan(now=NOW + timedelta(hours=1), tickers=["NVDA"])
    assert len(sent) == 1  # already told today
    agent.plan(now=NOW + timedelta(days=1), tickers=["NVDA"])
    assert len(sent) == 2


def test_other_failures_do_not_email(make_agent, monkeypatch):
    sent = _mails(monkeypatch)
    agent = make_agent({"NVDA": decision("NVDA", "Hold")})

    def analyze(ticker, trade_date, portfolio=None):
        if ticker == "AMD":
            raise TimeoutError("read timed out")
        return decision(ticker, "Hold")

    agent.analyze_fn = analyze
    plan = agent.plan(now=NOW, tickers=["NVDA", "AMD"])
    assert "out_of_credit" not in plan and sent == []


def test_dry_run_does_not_email(make_agent, monkeypatch):
    sent = _mails(monkeypatch)
    agent = make_agent()
    agent.analyze_fn = lambda t, d, portfolio=None: (_ for _ in ()).throw(CREDIT_ERROR)
    assert agent.plan(now=NOW, tickers=["NVDA"], dry_run=True)["out_of_credit"] is True
    assert sent == []


def test_unconfigured_smtp_is_a_logged_noop(settings):
    assert notify.send_email(settings.__class__(smtp_user="", smtp_password=""), "x", "y") is False
