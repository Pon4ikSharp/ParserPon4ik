from app import hydration_v2


def test_parse_domains():
    assert hydration_v2.parse_domains('"tass.ru,tass.com, ria.ru"') == (
        "tass.ru",
        "tass.com",
        "ria.ru",
    )


def test_domain_match_accepts_subdomain():
    domains = ("tass.ru",)
    assert hydration_v2.host_matches_domains("https://tass.ru/a", domains)
    assert hydration_v2.host_matches_domains("https://www.tass.ru/a", domains)


def test_domain_match_rejects_suffix_trick():
    domains = ("tass.ru",)
    assert not hydration_v2.host_matches_domains("https://eviltass.ru/a", domains)


def test_proxy_domain_filter():
    proxy = "http://user:pass@example.invalid:8080"
    domains = ("tass.ru",)
    assert hydration_v2.proxy_for_url("https://tass.ru/a", proxy, domains) == proxy
    assert hydration_v2.proxy_for_url("https://example.com/a", proxy, domains) is None


def test_proxy_empty_allowlist_preserves_legacy_all_behavior():
    proxy = "http://user:pass@example.invalid:8080"
    assert hydration_v2.proxy_for_url("https://example.com/a", proxy, ()) == proxy


def test_usable_extracted_text():
    basis, text = hydration_v2.usable_text(
        {"status": "extracted", "full_text": "A" * 500},
        "fallback",
    )
    assert basis == "hydration_v2"
    assert len(text) == 500


def test_partial_long_text_can_be_used_with_weaker_provenance():
    basis, text = hydration_v2.usable_text(
        {"status": "partial", "full_text": "B" * 500},
        "fallback",
    )
    assert basis == "hydration_v2_partial"
    assert len(text) == 500


def test_partial_short_text_falls_back():
    basis, text = hydration_v2.usable_text(
        {"status": "partial", "full_text": "short"},
        "fallback text",
    )
    assert basis == "source_text"
    assert text == "fallback text"


def test_error_is_retryable():
    assert hydration_v2.should_retry({"status": "error"})
    assert not hydration_v2.is_terminal({"status": "error"})


def test_paywall_is_terminal_hydration_outcome():
    assert hydration_v2.is_terminal({"status": "paywall"})
    assert not hydration_v2.should_retry({"status": "paywall"})


def test_http_403_is_terminal_blocked():
    class Response:
        status_code = 403

    class Error(Exception):
        response = Response()

    status, code = hydration_v2._exception_status(Error("forbidden"))
    assert status == "blocked"
    assert code == 403
    assert hydration_v2.is_terminal({"status": status})
    assert not hydration_v2.should_retry({"status": status})


def test_http_522_remains_retryable_error():
    class Response:
        status_code = 522

    class Error(Exception):
        response = Response()

    status, code = hydration_v2._exception_status(Error("timeout"))
    assert status == "error"
    assert code == 522
    assert hydration_v2.should_retry({"status": status})
