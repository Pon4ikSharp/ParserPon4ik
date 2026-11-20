from app import processing_v2


def test_initial_stage_is_prefilter_for_telegram():
    assert processing_v2.initial_stage({
        "source_kind": "telegram",
        "full_text": "",
        "content_status": "telegram",
    }) == "prefilter"


def test_initial_stage_is_prefilter_for_rss():
    assert processing_v2.initial_stage({
        "source_kind": "rss",
        "full_text": "",
        "content_status": "pending",
    }) == "prefilter"


def test_passed_unhydrated_rss_routes_to_hydration():
    assert processing_v2.next_stage_after_prefilter({
        "source_kind": "rss",
        "full_text": "",
        "content_status": "pending",
    }) == "hydrate"


def test_passed_hydrated_rss_routes_to_analysis():
    assert processing_v2.next_stage_after_prefilter({
        "source_kind": "rss",
        "full_text": "Full article",
        "content_status": "extracted",
    }) == "analyze"


def test_passed_telegram_routes_to_analysis():
    assert processing_v2.next_stage_after_prefilter({
        "source_kind": "telegram",
        "full_text": "",
        "content_status": "telegram",
    }) == "analyze"


def test_paywall_rss_uses_available_text_for_analysis():
    assert processing_v2.next_stage_after_prefilter({
        "source_kind": "rss",
        "full_text": "",
        "content_status": "paywall",
    }) == "analyze"


def test_pipeline_version_is_explicit():
    assert processing_v2.PIPELINE_VERSION.startswith("processing-v2.")
