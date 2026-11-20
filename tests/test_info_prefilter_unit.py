from app import info_prefilter


def test_watchlist_speaker_is_must_analyze():
    result = info_prefilter.classify(
        "Сергей Лавров заявил, что Запад продолжает давление на Россию.",
        source_title="ТАСС",
    )
    assert result.route == "MUST_ANALYZE"
    assert "lavrov" in result.actor_hits


def test_core_actor_mention_is_not_automatic_must():
    result = info_prefilter.classify(
        "В архиве опубликована фотография Владимира Путина за 2010 год."
    )
    assert result.route not in {"ANALYZE", "MUST_ANALYZE"}
    assert "putin" not in result.actor_hits


def test_official_kremlin_originator_putin_is_must_analyze():
    result = info_prefilter.classify(
        "Состоялся телефонный разговор Владимира Путина с иностранным лидером.",
        source_title="Сайт Президента России: Все материалы",
    )
    assert result.route == "MUST_ANALYZE"
    assert "official_originator:putin" in result.reason_codes


def test_official_kremlin_future_notice_is_not_must_analyze():
    result = info_prefilter.classify(
        "23 сентября Владимир Путин проведёт совещание с членами Правительства.",
        source_title="Сайт Президента России: Все материалы",
    )
    assert result.route != "MUST_ANALYZE"


def test_economic_collapse_label_is_detected():
    result = info_prefilter.classify(
        "Авторы утверждают, что Украину ждет экономический коллапс и страна-банкрот."
    )
    assert "economic_collapse_ua" in result.narrative_hits
    assert result.influence_score >= 5


def test_spread_can_upgrade_relevant_material():
    result = info_prefilter.classify(
        "Украина и НАТО обсуждают новый пакет военной помощи.",
        spread_count=20,
        unique_sources=12,
        cross_platform=True,
    )
    assert result.spread_score >= 8
    assert result.route in {"ANALYZE", "MUST_ANALYZE"}


def test_hashtags_are_extracted():
    assert "#дефолт" in info_prefilter.extract_hashtags("Текст #Украина #дефолт")


def test_same_source_repetition_does_not_inflate_spread():
    assert info_prefilter.spread_score(29, 1, False) == 0


def test_cross_source_spread_is_strong_signal():
    assert info_prefilter.spread_score(5, 5, False) >= 6
    assert info_prefilter.spread_score(5, 5, True) >= 8


def test_dynamic_term_is_hold_only_safety_net():
    base_text = (
        "Місцеве видання опублікувало звичайне повідомлення про відкриття "
        "нового громадського простору та графік роботи установи."
    )
    text = None
    base = None
    for index in range(100):
        candidate = f"{base_text} Номер повідомлення {index}."
        result = info_prefilter.classify(candidate)
        if result.route == "DROP_SOFT":
            text, base = candidate, result
            break
    assert text is not None and base is not None
    rescued = info_prefilter.classify(text, dynamic_terms=("#редакційний_ярлик",))
    assert rescued.route == "HOLD"
    assert rescued.influence_score == base.influence_score
    assert "dynamic_lexicon_hold" in rescued.reason_codes


def test_dynamic_term_cannot_upgrade_hold_to_analyze():
    text = "Україна і НАТО провели звичайну робочу зустріч представників установ."
    base = info_prefilter.classify(text)
    rescued = info_prefilter.classify(text, dynamic_terms=("#редакційний_ярлик",))
    assert base.route not in {"ANALYZE", "MUST_ANALYZE"}
    assert rescued.route == "HOLD"
    assert rescued.influence_score == base.influence_score


def test_same_source_repetition_has_no_spread_score():
    assert info_prefilter.spread_score(29, 1, False) == 0


def test_cross_source_spread_is_strong_signal():
    assert info_prefilter.spread_score(10, 5, False) == 6
    assert info_prefilter.spread_score(10, 5, True) == 8
