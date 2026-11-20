from app import prefilter_v2


def test_empty_drops():
    result = prefilter_v2.classify("   \n  ")
    assert result.decision == "drop"
    assert result.reason == "empty_or_nontext"


def test_link_only_drops():
    result = prefilter_v2.classify("https://example.com")
    assert result.decision == "drop"
    assert result.reason == "link_only"


def test_short_but_meaningful_news_passes():
    result = prefilter_v2.classify("Лавров прибув до Мінська на переговори.")
    assert result.decision == "pass"


def test_subscribe_footer_does_not_drop_real_news():
    result = prefilter_v2.classify(
        "Російські посадовці заявили про новий раунд переговорів у Мінську. "
        "Зустріч має відбутися завтра. Підписуйтеся на наш канал."
    )
    assert result.decision == "pass"


def test_promotional_only_drops():
    result = prefilter_v2.classify(
        "Підписуйтеся на наш канал. По вопросам рекламы: https://example.com"
    )
    assert result.decision == "drop"
    assert result.reason == "technical_or_promotional_only"


def test_exact_key_ignores_spacing_and_case():
    assert prefilter_v2.exact_key("Hello   WORLD") == prefilter_v2.exact_key("hello world")


def test_near_duplicate_detects_small_edit():
    a = prefilter_v2.classify(
        "Міністр закордонних справ заявив про початок нового раунду переговорів "
        "між сторонами наступного тижня у столиці держави."
    ).as_dict()
    b = prefilter_v2.classify(
        "Міністр закордонних справ заявив про початок нового раунду переговорів "
        "між сторонами вже наступного тижня у столиці держави."
    ).as_dict()
    result = prefilter_v2.find_near_duplicates([a, b], threshold=0.80)
    assert 1 in result


def test_choose_input_prefers_full_text():
    basis, text = prefilter_v2.choose_input({
        "source_text": "short",
        "feed_text": "feed",
        "full_text": "full article",
    })
    assert basis == "full_text"
    assert text == "full article"
