from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, asdict

from app import primary_dedup
from app.info_multilingual_lexicon import (
    FOREIGN_INFLUENCE_GROUPS, SOURCE_PROFILE_HINTS, MULTILINGUAL_NARRATIVE_FAMILIES,
)
from app.info_claim_patterns import UKRAINE_SUBJECTS, ISSUE_CLAIM_RULES

RULES_VERSION = "info-prefilter-rules-v18.2-20260930"

HASHTAG_RE = re.compile(r"(?<!\w)#[\w\-]+", re.UNICODE)
SPACE_RE = re.compile(r"\s+")
SPEECH_VERBS = (
    "заявил", "заявила", "заявив", "заявила", "сказал", "сказала", "сказав",
    "сообщил", "сообщила", "повідомив", "повідомила", "отметил", "отметила",
    "написал", "написала", "написав", "назвал", "назвала", "назвав",
    "обвинил", "обвинила", "звинуватив", "предупредил", "попередив",
    "подчеркнул", "наголосив", "утверждает", "стверджує", "said", "stated",
    "claimed", "warned", "accused", "according to",
)

ACTORS = {
    "putin": ("владимир путин", "володимир путін", "путин", "путін", "vladimir putin", "władimir putin", "vladimir poutine", "putin", "poutine", "فلاديمير بوتين", "بوتين"),
    "lavrov": ("сергей лавров", "сергій лавров", "лавров", "sergey lavrov", "sergei lavrov", "lavrov", "lavrow", "سيرغي لافروف", "لافروف"),
    "zakharova": ("мария захарова", "марии захаровой", "марией захаровой", "марию захарову", "марія захарова", "захарова", "maria zakharova", "maria zacharova", "maria zakharowa", "zakharova", "zacharova", "ماريا زاخاروفا", "زاخاروفا"),
    "peskov": ("дмитрий песков", "дмитро пєсков", "дмитро песков", "песков", "пєсков", "dmitry peskov", "dmitri peskov", "peskov", "peskow", "دميتري بيسكوف", "بيسكوف"),
    "medvedev": ("дмитрий медведев", "дмитро медведєв", "медведев", "медведєв", "dmitry medvedev", "medvedev", "medwedew", "دميتري ميدفيديف", "ميدفيديف"),
    "nebenzya": ("василий небензя", "василия небензи", "василию небензе", "василием небензей", "василь небензя", "небензя", "небензи", "небензе", "небензю", "небензей", "vasily nebenzya", "vassily nebenzia", "nebenzya", "nebenzia", "فاسيلي نيبينزيا", "نيبينزيا"),
    "matvienko": ("валентина матвиенко", "валентина матвієнко", "матвиенко", "матвієнко", "valentina matvienko", "matvienko", "matviyenko", "فالنتينا ماتفيينكو", "ماتفيينكو"),
    "volodin": ("вячеслав володин", "в'ячеслав володін", "володин", "володін"),
    "shoigu": ("сергей шойгу", "сергій шойгу", "шойгу"),
    "patrushev": ("николай патрушев", "микола патрушев", "патрушев"),
    "belousov": ("андрей белоусов", "андрій білоусов", "белоусов", "білоусов"),
    "naryshkin": ("сергей нарышкин", "сергій наришкін", "нарышкин", "наришкін"),
    "bortnikov": ("александр бортников", "олександр бортніков", "бортников", "бортніков"),
    "medinsky": ("владимир мединский", "володимир мединський", "мединский", "мединський"),
    "ryabkov": ("сергей рябков", "сергій рябков", "рябков"),
    "polyansky": ("дмитрий полянский", "дмитро полянський", "полянский", "полянський"),
    "miroshnik": ("родион мирошник", "родіон мірошник", "мирошник", "мірошник"),
    "gerasimov": ("валерий герасимов", "валерій герасимов", "герасимов"),
    "dmitriev": ("кирилл дмитриев", "кирил дмитрієв", "kirill dmitriev", "дмитриев", "дмитрієв"),
    "simonyan": ("маргарита симоньян", "margarita simonyan", "симоньян", "simonyan"),
    "solovyov": ("владимир соловьев", "володимир соловйов", "vladimir solovyov", "соловьев", "соловйов", "solovyov"),
    "kadyrov": ("рамзан кадыров", "рамзан кадиров", "ramzan kadyrov", "кадыров", "кадиров", "kadyrov"),
    "dugin": ("александр дугин", "олександр дугін", "alexander dugin", "дугин", "дугін", "dugin"),
}

CORE_ACTORS_ALWAYS = {"putin", "lavrov", "zakharova", "peskov", "medvedev", "nebenzya", "matvienko"}

DOMAIN_TERMS = {
    "украина": 4, "україна": 4, "украин": 3, "україн": 3, "киев": 2, "київ": 2,
    "зеленск": 4, "всу": 4, "зсу": 4, "мобилизац": 3, "мобілізац": 3,
    "тцк": 4, "тцкаш": 4, "людолов": 4, "бусиф": 4, "могилиз": 4,
    "ухилян": 3, "уклонист": 3, "просрочк": 3,
    "россия": 2, "росія": 2, "кремл": 2, "москва": 1, "нато": 3,
    "евросоюз": 2, "євросоюз": 2, "ес": 1, "санкц": 2, "переговор": 2,
    "перемир": 2, "войн": 2, "війна": 2, "фронт": 2, "оборона": 2,
    "сша": 2, "трамп": 2, "европа": 1, "європа": 1, "молд": 2,
    "банковая": 4, "офис президента": 4, "офіс президента": 4,
    "зе власть": 3, "зе-власть": 3, "зе режим": 3, "зе-режим": 3,
    "ukraine": 4, "ukraina": 4, "ukrajina": 4, "ucrania": 4, "ukrajna": 4,
    "україни": 3, "ukrain": 3, "ukraiń": 3, "ukrajinsk": 3,
    "russia": 2, "russie": 2, "rosja": 2, "rusko": 2, "rusia": 2,
    "oroszország": 2, "russland": 2,
    "zelensky": 4, "zelenski": 4, "zelenskyy": 4, "zełenski": 4,
    "zelenskyj": 4, "selenskyj": 4, "zelenszkij": 4,
    "kyiv": 2, "kiev": 2, "kijów": 2, "kyjev": 2,
    "nato": 3, "otan": 3,
    "war": 2, "guerre": 2, "wojna": 2, "vojna": 2, "válka": 2, "guerra": 2,
    "mobilization": 3, "mobilisation": 3, "mobilizacja": 3, "mobilizácia": 3,
    "sanctions": 2, "sankcje": 2, "sankcie": 2, "sankce": 2,
    "european union": 2, "union européenne": 2, "unia europejska": 2,
    "európska únia": 2, "europäische union": 2,
    "أوكرانيا": 4, "روسيا": 2, "زيلينسكي": 4, "الناتو": 3,
}

NARRATIVE_FAMILIES = {
    "economic_collapse_ua": (
        5,
        (
            "крах экономики укра", "крах української економ", "экономический коллапс",
            "економічний колапс", "страна-банкрот", "країна-банкрут",
            "экономика украины рух", "економіка україни рух", "гривна рухнет",
            "бюджетная дыра", "бюджет украины развал", "экономика держится только",
            "без западных денег укра", "украина без денег", "україна без грошей",
            "экономагеддон", "економагеддон", "черная зима", "чёрная зима", "чорна зима",
            "за все заплатят украинцы", "за все заплатять українці",
            "убытки для бизнеса и экономики", "збитки для бізнесу та економіки",
            "цены на продукты вырастут", "ціни на продукти зростуть",
            "вас будут обдирать", "вас будуть обдирати",
        ),
    ),
    "state_failure": (
        4,
        (
            "failed state", "несостоявшееся государство", "держава не відбулася",
            "государство разваливается", "страна разваливается", "країна розвалюється",
            "киев потерял контроль", "київ втратив контроль", "власть потеряла контроль",
        ),
    ),
    "delegitimization": (
        4,
        (
            "киевский режим", "режим зеленского", "режим зеленського", "хунта",
            "нелегитимный зелен", "нелегітимний зелен", "марионеточный режим",
            "маріонетковий режим", "просроченный президент",
        ),
    ),
    "trust_erosion": (
        4,
        (
            "власти скрывают", "влада приховує", "сми скрывают", "змі приховують",
            "сми молчат", "змі мовчать", "вам не покажут", "вам не покажуть",
            "официальная пропаганда врет", "западные сми врут", "західні змі брешуть",
            "западные сми замалчивают", "західні змі замовчують",
            "западная пресса", "західна преса", "обслугой интересов",
            "выдумывают истории", "вигадують історії",
        ),
    ),
    "external_control": (
        4,
        (
            "внешнее управление", "зовнішнє управління", "кураторы из вашингтона",
            "вашингтон управляет", "запад управляет украиной", "захід керує україною",
            "марионетка запада", "маріонетка заходу", "глобалисты", "глобалісти",
            "западные кураторы", "західні куратори",
        ),
    ),
    "west_abandonment": (
        4,
        (
            "запад бросил украину", "захід кинув україну", "украину сливают",
            "україну зливають", "запад устал от украины", "захід втомився від україни",
            "помощь прекратится", "допомога припиниться",
        ),
    ),
    "military_collapse": (
        4,
        (
            "всу разваливаются", "зсу розвалюються", "фронт рухнул", "фронт обвалился",
            "оборона рухнула", "оборона посыпалась", "массово сдаются", "масово здаються",
            "украинская армия уничтожена", "українська армія знищена",
        ),
    ),
    "mobilization_failure": (
        3,
        (
            "насильственная мобилизация", "насильницька мобілізація", "могилизация",
            "людей хватают на улицах", "людей хапають на вулицях", "тцк охотятся",
            "тцк полюють", "бусификац",
        ),
    ),
    "energy_collapse": (
        3,
        (
            "энергетический коллапс", "енергетичний колапс", "энергосистема рухнула",
            "енергосистема зруйнована", "украина останется без света", "україна залишиться без світла",
        ),
    ),
    "war_prolongation_blaming": (
        4,
        (
            "затянуть войну", "затягнути війну", "война до последнего украинца",
            "війна до останнього українця", "зеленский не хочет мира",
            "зеленський не хоче миру", "киев не хочет мира", "київ не хоче миру",
            "срывает переговоры", "зриває переговори",
        ),
    ),
    "corruption_totalization": (
        3,
        (
            "тотальная коррупция", "тотальна корупція", "коррупционный режим",
            "корупційний режим", "обогащаются на войне", "збагачуються на війні",
            "воруют западную помощь", "крадуть західну допомогу",
        ),
    ),
    "conspiracy_hidden_truth": (
        3,
        (
            "на самом деле", "насправді", "правда которую скрывают", "правда яку приховують",
            "тайный план", "таємний план", "за этим стоит", "за цим стоїть",
            "все было спланировано", "все було сплановано",
        ),
    ),
    "historical_nazi": (
        4,
        (
            "нацистский режим", "нацистський режим", "бандеровский режим",
            "бандерівський режим", "неонацисты в киеве", "неонацисти в києві",
        ),
    ),
}

HASHTAG_SIGNAL_STEMS = (
    "крах", "коллапс", "колапс", "дефолт", "катастроф", "развал", "розвал",
    "экономагеддон", "економагеддон", "бусиф", "могилиз", "диктатур",
    "хунт", "капитуляц", "капітуляц",
)

CHANNEL_LEXICONS = {
    "легитимный": (
        "экономагеддон", "черная зима", "чёрная зима",
        "за все заплатят украинцы", "западные сми замалчивают",
        "глобалисты", "зе-авантюр", "вас будут обдирать",
        "репрессии зе власти", "экономики страны",
    ),
}


def channel_signal_score(source_title: str, text: str) -> tuple[int, tuple[str, ...]]:
    source = _norm(source_title)
    body = _norm(text)
    hits = []
    for channel, phrases in CHANNEL_LEXICONS.items():
        if channel not in source:
            continue
        for phrase in phrases:
            if phrase in body:
                hits.append(phrase)
    return min(6, len(hits) * 2), tuple(dict.fromkeys(hits))


TELEGRAM_SOURCE_PROFILE_HINTS = {
    "pseudo_ua": ("легитимный", "зерада", "картель", "женщина с косой", "насправд",
                  "резидент", "сплетница", "другая украина", "ukr leaks", "время.ua",
                  "будни ухилянта", "анти тцк"),
    "official_military": ("минобороны россии",),
    "military_amplifier": ("операция z", "военкоры русской весны", "readovka",
        "рыбарь", "wargonzo", "два майора", "kotsnews", "военкор котенок",
        "поддубный", "colonelcassad", "борис рожин", "военный осведомитель"),
    "official_diplomatic": ("мид россии", "мария захарова", "постпредство россии при оон"),
    "official_political": ("совет федерации", "вячеслав володин", "государственная дума"),
    "factcheck_amplifier": ("война с фейками",),
    "amplifier": ("соловьев", "rt на русском", "незыгарь"),
}

PSEUDO_UA_SIGNAL_STEMS = (
    "тцк", "тцкаш", "людолов", "бусиф", "могилиз", "ухилян", "уклонист",
    "просроч", "банковая", "офис президента", "зе-власть", "зе власть", "зе режим",
    "эскалац", "екскалац", "экономагеддон", "черная зима", "чорна зима",
    "перемог", "зрад", "мобилизац", "мобілізац", "коррупц", "корупц",
    "неизбран", "нелегитим", "узурп", "диктат", "тиран", "репресс",
    "на убой", "мясн", "катастроф", "коллапс", "обвал", "рухнет",
    "нечем закрывать", "нечем отбиват", "останется без", "залишиться без",
    "по слухам", "слух", "расклад", "инсайд", "обдирать",
    "отопительный сезон", "долги предприятий", "финансами близка к критической",
)

MILITARY_CLAIM_STEMS = (
    "взят под контроль", "установлен контроль", "взял", "взявш",
    "освобож", "побед", "уничтож", "ликвидир", "поражен", "поразил",
    "разнес", "выжига", "сжига", "утюжат", "сбил", "сбито", "перехват",
    "окруж", "кольц", "прорыв", "продвиж", "нанес", "нанос", "удар по",
    "ударов по", "отработал по", "зачист", "захвачен",
)
MILITARY_UA_CONTEXT_STEMS = (
    "украин", "всу", "зсу", "днр", "лнр", "зона сво", "спецоперац", "харьков", "харків", "сумск",
    "сумськ", "запорож", "запоріж", "добропол", "краматор", "славян", "слов'ян",
    "константинов", "орехов", "одесс", "одес", "киев", "київ", "противник", "боевик",
    "фламинго", "storm shadow", "atacms",
)

FACTCHECK_STEMS = ("фейк", "вброс", "дезинформац", "манипуляц", "ложь", "неправда")
FACTCHECK_CONTEXT_STEMS = ("украин", "всу", "зсу", "зеленск", "киев", "київ", "запад", "нато", "сша", "европ")

DIPLOMATIC_STRONG_STEMS = (
    "киевский режим", "київський режим", "русофоб", "двойные стандарт",
    "фальсификац", "буч", "нацист", "фашист", "гитлер",
)

DIPLOMATIC_FRAMING_STEMS = (
    "запад", "нато", "санкц", "киевский режим", "київський режим", "русофоб",
    "колониал", "многополяр", "багатополяр", "односторонн", "двойные стандарт",
    "террорист", "провокац", "фальсификац", "буч", "нацист", "фашист", "гитлер",
    "вмешательств", "глобального юга", "мирового большинства", "миропоряд",
)

SOURCE_INFLUENCE_PRIORS = {
    # news-pravda.com localisations are distribution/relay nodes, not
    # independent origin sources. Their presence is tracked as propagation
    # metadata and must not by itself upgrade a document to ANALYZE.
    "украина.ру": 3,
    "ukraina.ru": 3,
    "south front": 2,
    "rt en español": 1,
    "rt на русском": 1,
    "rt arabic": 1,
    "sputnik": 1,
    "cz24.news": 1,
    "infokurýr": 1,
}


def source_influence_prior(source_title: str) -> tuple[int, str]:
    source = _norm(source_title)
    best = 0
    label = ""
    for needle, score in SOURCE_INFLUENCE_PRIORS.items():
        if needle in source and score > best:
            best = score
            label = needle
    return best, label


def telegram_source_profile(source_title: str) -> str:
    source = _norm(source_title)
    for profile, hints in TELEGRAM_SOURCE_PROFILE_HINTS.items():
        if any(hint in source for hint in hints):
            return profile
    return ""


def telegram_recall_signals(text: str, title: str, source_title: str, source_kind: str):
    if str(source_kind or "").lower() != "telegram":
        return 0, (), ()
    profile = telegram_source_profile(source_title)
    if not profile:
        return 0, (), ()
    norm = _norm(f"{title} {text}")
    score = 0
    families = []
    reasons = [f"telegram_profile:{profile}"]
    if profile == "pseudo_ua":
        hits = tuple(stem for stem in PSEUDO_UA_SIGNAL_STEMS if stem in norm)
        if hits:
            score = min(6, 2 + min(4, len(hits) * 2))
            families.append("telegram_pseudo_ua_signal")
            reasons.append("pseudo_ua:" + ",".join(hits[:8]))
    if profile in {"official_military", "military_amplifier"}:
        claims = tuple(stem for stem in MILITARY_CLAIM_STEMS if stem in norm)
        context = tuple(stem for stem in MILITARY_UA_CONTEXT_STEMS if stem in norm)
        if claims and context:
            score = max(score, 5)
            families.append("military_success_claim")
            reasons.append("military_claim:" + claims[0] + "+" + context[0])
    if profile == "factcheck_amplifier":
        fhits = tuple(stem for stem in FACTCHECK_STEMS if stem in norm)
        fctx = tuple(stem for stem in FACTCHECK_CONTEXT_STEMS if _contains_signal(norm, stem))
        if fhits and fctx:
            score = max(score, 5)
            families.append("factcheck_framing")
            reasons.append("factcheck:" + fhits[0] + "+" + fctx[0])
    if profile in {"official_diplomatic", "official_political", "amplifier"}:
        hits = tuple(stem for stem in DIPLOMATIC_FRAMING_STEMS if _contains_signal(norm, stem))
        if hits:
            strong = any(_contains_signal(norm, stem) for stem in DIPLOMATIC_STRONG_STEMS)
            framing_score = 5 if (len(hits) >= 2 or strong) else (4 if profile != "amplifier" else 2)
            score = max(score, framing_score)
            families.append("official_framing" if profile != "amplifier" else "amplifier_framing")
            reasons.append("framing:" + ",".join(hits[:8]))
    return score, tuple(dict.fromkeys(families)), tuple(reasons)


def relay_network_label(source_title: str) -> str:
    source = _norm(source_title)
    if source.startswith("pravda ") or source == "pravda":
        return "pravda_network"
    return ""


CLAIM_PATTERNS = (
    "заявил, что", "заявила, что", "заявив, що", "заявила, що",
    "обвинил", "обвинила", "звинуватив", "по словам", "за словами",
    "утверждает, что", "стверджує, що", "на самом деле", "насправді",
    "это доказывает", "це доводить", "причиной является", "причиною є",
    "за этим стоит", "за цим стоїть", "цель состоит", "мета полягає",
)

NEGATIVE_LOW_VALUE = (
    "курс валют", "прогноз погоды", "прогноз погоди", "результаты матча",
    "результат матчу", "расписание матчей", "розклад матчів",
)


def low_value_protocol(text: str, title: str = "") -> bool:
    norm = _norm(f"{title} {text}")[:1800]
    birthday = (
        ("поздрав" in norm and any(x in norm for x in ("днем рождения", "летием", "юбиле")))
        or ("привітав" in norm and any(x in norm for x in ("днем народження", "ювіле")))
    )
    youth_festival = any(x in norm for x in (
        "фестиваль молодеж", "молодежный фестиваль",
        "фестиваль молоді", "молодіжний фестиваль",
    ))
    return birthday or youth_festival


OFFICIAL_ACTOR_SOURCE_MARKERS = {
    "putin": ("сайт президента россии", "кремлін"),
}

FUTURE_EVENT_NOTICE_MARKERS = (
    "проведет", "проведёт", "состоится", "запланирован", "запланирована",
    "предстоит", "планирует", "проведе", "відбудеться", "заплановано",
)


def official_actor_originator_hits(
    source_title: str,
    text: str,
    title: str,
    mentions: tuple[str, ...],
) -> tuple[str, ...]:
    """Select substantive official-originator items without calling a mention speech."""
    source = _norm(source_title)
    norm = _norm(f"{title} {text}")[:1400]
    if any(re.search(rf"\b{re.escape(marker)}\b", norm) for marker in FUTURE_EVENT_NOTICE_MARKERS):
        return ()
    hits = []
    for actor, markers in OFFICIAL_ACTOR_SOURCE_MARKERS.items():
        if actor in mentions and any(marker in source for marker in markers):
            hits.append(actor)
    return tuple(hits)


@dataclass(frozen=True)
class PrefilterResult:
    route: str
    hard_filter_reason: str
    domain_score: int
    influence_score: int
    spread_score: int
    actor_hits: tuple[str, ...]
    narrative_hits: tuple[str, ...]
    hashtag_hits: tuple[str, ...]
    reason_codes: tuple[str, ...]

    def as_dict(self):
        return asdict(self)


def _norm(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").lower().replace("ё", "е")
    return SPACE_RE.sub(" ", value).strip()


def extract_hashtags(text: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(tag.lower() for tag in HASHTAG_RE.findall(text or "")))


SPEAKER_VERBS = tuple(dict.fromkeys((*SPEECH_VERBS,
    "анонсировал", "анонсировала", "анонсував", "анонсувала",
    "прокомментировал", "прокомментировала", "прокоментував", "прокоментувала",
    "рассказал", "рассказала", "розповів", "розповіла",
    "подтвердил", "подтвердила", "підтвердив", "підтвердила",
    "призвал", "призвала", "закликав", "закликала",
    "объяснил", "объяснила", "пояснив", "пояснила",
    "считает", "вважає", "выразил", "выразила", "висловив", "висловила",
    "добавил", "добавила", "додав", "додала",
    "указал", "указала", "вказав", "вказала",
    "напомнил", "напомнила", "нагадав", "нагадала",
    "announced", "commented", "noted", "wrote", "added", "told",
)))

SPEAKER_REPORTING_PREFIXES = (
    "по словам ", "за словами ", "according to ",
    "как заявил ", "як заявив ", "как сказала ", "як сказала ",
    "как сообщил ", "як повідомив ", "как отметила ", "как отметил ",
)
SPEAKER_CLAUSE_BLOCKERS = (
    " что ", " що ", " that ", " чтобы ", " щоб ",
    " о ", " об ", " про ", " about ",
)
SPEAKER_OBJECT_BLOCKERS = (
    "выступлен", "виступ", "речь", "промов", "встреч", "зустріч",
    "участи", "участі", "слова", "заявлен", "позици", "позиці",
)
SPEAKER_ROLE_CUES = (
    "президент", "министр", "міністр", "глава", "представитель",
    "представник", "пресс-секретарь", "речник", "посол", "посол ",
    "зампред", "председатель", "голова", "секретарь", "дипломат",
    "лидер", "спикер",
)

SPEAKER_DATIVE_FORMS = {
    "putin": ("путину", "путіну"),
    "lavrov": ("лаврову",),
    "peskov": ("пескову", "пєскову"),
    "medvedev": ("медведеву", "медведєву"),
    "volodin": ("володину", "володіну"),
    "patrushev": ("патрушеву",),
    "belousov": ("белоусову", "білоусову"),
    "naryshkin": ("нарышкину", "наришкіну"),
    "bortnikov": ("бортникову", "бортнікову"),
    "medinsky": ("мединскому", "мединському"),
    "ryabkov": ("рябкову",),
    "polyansky": ("полянскому", "полянському"),
    "miroshnik": ("мирошнику", "мірошнику"),
    "gerasimov": ("герасимову",),
    "dmitriev": ("дмитриеву", "дмитрієву"),
    "kadyrov": ("кадырову", "кадирову"),
    "dugin": ("дугину", "дугіну"),
}

CORE_ACTOR_REGEX_TEXT = {
    "putin": (
        r"(?<!\w)путин(?:а|у|ым|е)?(?!\w)",
        r"(?<!\w)путін(?:а|у|ом|і)?(?!\w)",
        r"(?<!\w)putin(?!\w)", r"(?<!\w)poutine(?!\w)",
    ),
    "lavrov": (
        r"(?<!\w)лавров(?:а|у|ым|е)?(?!\w)",
        r"(?<!\w)lavrov(?!\w)", r"(?<!\w)lavrow(?!\w)",
    ),
    "zakharova": (
        r"(?<!\w)захаров(?:а|ой|у|ою)(?!\w)",
        r"(?<!\w)zakharova(?!\w)", r"(?<!\w)zacharova(?!\w)",
        r"(?<!\w)zacharowa(?!\w)",
    ),
    "peskov": (
        r"(?<!\w)песков(?:а|у|ым|е)?(?!\w)",
        r"(?<!\w)пєсков(?:а|у|ом|і)?(?!\w)",
        r"(?<!\w)peskov(?!\w)", r"(?<!\w)peskow(?!\w)",
    ),
    "medvedev": (
        r"(?<!\w)медведев(?:а|у|ым|е)?(?!\w)",
        r"(?<!\w)медведєв(?:а|у|ом|і)?(?!\w)",
        r"(?<!\w)medvedev(?!\w)", r"(?<!\w)medwedew(?!\w)",
    ),
    "nebenzya": (
        r"(?<!\w)небенз(?:я|и|е|ю|ей)(?!\w)",
        r"(?<!\w)nebenzy?a(?!\w)", r"(?<!\w)nebenzia(?!\w)",
    ),
    "matvienko": (
        r"(?<!\w)матвиенко(?!\w)", r"(?<!\w)матвієнко(?!\w)",
        r"(?<!\w)matvi(?:enko|yenko)(?!\w)",
    ),
}

ACTOR_NEGATIVE_CONTEXT = {
    "volodin": ("василиса володина", "василисы володиной"),
    "medvedev": (
        "андрей медведев", "андрея медведева", "андрій медведєв",
        "андрій медведєв", "andrey medvedev", "andrei medvedev",
    ),
}


def _build_actor_regexes():
    compiled = {}
    for actor, aliases in ACTORS.items():
        patterns = CORE_ACTOR_REGEX_TEXT.get(actor)
        if patterns is None:
            patterns = tuple(
                r"(?<!\w)" + re.escape(_norm(alias)) + r"(?!\w)"
                for alias in aliases if len(_norm(alias)) >= 4
            )
        compiled[actor] = tuple(re.compile(pattern) for pattern in patterns)
    return compiled


ACTOR_REGEXES = _build_actor_regexes()


def _actor_occurrences(norm: str, actor: str) -> tuple[tuple[int, int], ...]:
    matches = []
    negatives = ACTOR_NEGATIVE_CONTEXT.get(actor, ())
    for pattern in ACTOR_REGEXES.get(actor, ()):
        for match in pattern.finditer(norm):
            window = norm[max(0, match.start() - 30):min(len(norm), match.end() + 30)]
            if any(value in window for value in negatives):
                continue
            matches.append((match.start(), match.end()))
    return tuple(sorted(set(matches)))


def actor_mention_hits(text: str, title: str = "") -> tuple[str, ...]:
    norm = _norm(f"{title} {text}")
    return tuple(actor for actor in ACTORS if _actor_occurrences(norm, actor))


def _contains_other_actor(fragment: str, actor: str) -> bool:
    return any(
        other != actor and _actor_occurrences(fragment, other)
        for other in ACTORS
    )


def actor_speaker_hits(
    text: str,
    title: str = "",
    source_title: str = "",
) -> tuple[str, ...]:
    # Attribution is intentionally limited to the headline and opening
    # paragraph. Scraped pages often append recommendations and unrelated
    # headlines later in full_text, which must not create speaker hits.
    head = _norm(title)
    body = _norm(text)[:700]
    norm = f"{head} . {body}".strip()
    source = _norm(source_title)
    hits = []

    for actor in ACTORS:
        occurrences = _actor_occurrences(norm, actor)
        if not occurrences:
            continue

        # A personal source/channel named after the actor is a strong signal.
        if _actor_occurrences(source, actor):
            hits.append(actor)
            continue

        contextual = False
        for start, end in occurrences:
            before = norm[max(0, start - 100):start]
            after = norm[end:min(len(norm), end + 100)]
            surface = norm[start:end]

            # A dative surname is normally the addressee/object, not the speaker:
            # "Путину напомнили...", "Лаврову сообщили...".
            if surface in SPEAKER_DATIVE_FORMS.get(actor, ()):
                continue

            # Headline quotation form: "Медведев: «...»".
            if start < len(head) + 3 and re.match(r"^\s*[:—-]\s*[«\"“]?", after):
                contextual = True
                break

            if any(before.endswith(prefix) for prefix in SPEAKER_REPORTING_PREFIXES):
                contextual = True
                break

            # Safest direction: actor first, reporting verb shortly after.
            for verb in SPEAKER_VERBS:
                verb_pos = after.find(verb)
                if 0 <= verb_pos <= 30:
                    gap = after[:verb_pos]
                    if (
                        not any(mark in gap for mark in (". ", "! ", "? ", "; "))
                        and not _contains_other_actor(gap, actor)
                    ):
                        contextual = True
                        break
            if contextual:
                break

            # Inverted attribution: "заявил Песков" or
            # "заявил пресс-секретарь президента Дмитрий Песков".
            for verb in SPEAKER_VERBS:
                verb_pos = before.rfind(verb)
                if verb_pos < 0:
                    continue
                gap = before[verb_pos + len(verb):]
                padded = f" {gap} "
                if any(blocker in padded for blocker in SPEAKER_CLAUSE_BLOCKERS):
                    continue
                if any(blocker in gap for blocker in SPEAKER_OBJECT_BLOCKERS):
                    continue
                if any(mark in gap for mark in (". ", "! ", "? ", "; ")):
                    continue
                direct = len(gap) <= 18
                titled = len(gap) <= 75 and any(cue in gap for cue in SPEAKER_ROLE_CUES)
                if (direct or titled) and not _contains_other_actor(gap, actor):
                    contextual = True
                    break
            if contextual:
                break

        if contextual:
            hits.append(actor)

    # News headlines can misattribute an announcement while the opening body
    # explicitly says who actually reported it. For announcement headlines,
    # prefer a direct "об этом сообщила/заявила X" attribution in the body.
    if len(hits) > 1 and "анонс" in head:
        body_explicit = []
        attribution_markers = (
            "об этом сообщил", "об этом сообщила", "об этом заявил", "об этом заявила",
            "про це повідомив", "про це повідомила", "про це заявив", "про це заявила",
        )
        for actor in hits:
            for start, _ in _actor_occurrences(body, actor):
                before = body[max(0, start - 90):start]
                if any(marker in before for marker in attribution_markers):
                    body_explicit.append(actor)
                    break
        if body_explicit:
            hits = list(dict.fromkeys(body_explicit))

    return tuple(hits)


def select_prefilter_text(source_text: str = "", full_text: str = "", feed_text: str = "") -> str:
    source = (source_text or "").strip()
    full = (full_text or "").strip()
    feed = (feed_text or "").strip()
    if len(source) >= 160:
        return source
    if len(full) >= 160:
        return full
    return source or full or feed


EXACT_SIGNAL_TERMS = {"нато", "nato", "otan", "сша", "всу", "зсу", "ес", "war"}


def _contains_signal(haystack: str, needle: str) -> bool:
    term = (needle or "").strip()
    if not term:
        return False
    if term in EXACT_SIGNAL_TERMS:
        return bool(re.search(rf"(?<!\w){re.escape(term)}(?!\w)", haystack))
    return term in haystack


def domain_score(text: str, title: str = "") -> tuple[int, tuple[str, ...]]:
    body = _norm(text)
    head = _norm(title)
    score = 0
    hits = []
    for term, weight in DOMAIN_TERMS.items():
        if _contains_signal(head, term):
            score += weight * 2
            hits.append(term.strip())
        elif _contains_signal(body, term):
            score += weight
            hits.append(term.strip())
    return min(score, 20), tuple(dict.fromkeys(hits))


def _strip_source_boilerplate(value: str, source_title: str = "") -> str:
    source = _norm(source_title)
    cleaned = re.sub(
        r"(?:^|\s)насправді[.!]?\s*(?:подписаться|підписатися)\s*$",
        " ", value or "", flags=re.IGNORECASE,
    )
    if source in {"насправді", "насправдi"}:
        cleaned = re.sub(r"(?:^|\s)насправді[.!]?\s*$", " ", cleaned, flags=re.IGNORECASE)
    return cleaned


def influence_signals(text: str, title: str = "") -> tuple[int, tuple[str, ...], tuple[str, ...]]:
    norm = _norm(f"{title} {text}")
    score = 0
    families = []
    reasons = []
    for family, (weight, phrases) in NARRATIVE_FAMILIES.items():
        matched = next((p for p in phrases if p in norm), None)
        if matched:
            families.append(family)
            score += weight
            reasons.append(f"{family}:{matched}")
    claim_hits = sum(1 for p in CLAIM_PATTERNS if p in norm)
    if claim_hits:
        score += min(3, claim_hits)
        reasons.append(f"claim_patterns:{claim_hits}")
    if any(p in norm for p in NEGATIVE_LOW_VALUE) and not families:
        score = max(0, score - 2)
        reasons.append("low_value_modifier")
    return min(score, 20), tuple(families), tuple(reasons)


def issue_claim_signals(text: str, title: str = "") -> tuple[tuple[str, ...], tuple[str, ...]]:
    raw = f"{title}\n{text}"
    segments = [_norm(part) for part in re.split(r"[\n.!?;]+", raw) if part.strip()]
    families = []
    reasons = []
    for family, (_weight, issue_terms, pressure_terms) in ISSUE_CLAIM_RULES.items():
        matched = None
        for segment in segments:
            subject = next((term for term in UKRAINE_SUBJECTS if term in segment), None)
            if not subject:
                continue
            issue = next((term for term in issue_terms if term in segment), None)
            pressure = next((term for term in pressure_terms if term in segment), None)
            if not issue or not pressure:
                continue
            if family == "energy_collapse":
                subject_pos = segment.find(subject)
                pressure_pos = segment.find(pressure)
                if subject_pos > pressure_pos:
                    continue
                if pressure_pos - subject_pos > 90:
                    continue
            matched = (issue, pressure)
            break
        if matched:
            families.append(family)
            reasons.append(f"claim_cooccurrence:{family}:{matched[0]}+{matched[1]}")
    return tuple(dict.fromkeys(families)), tuple(reasons)


def multilingual_influence_signals(text: str, title: str = "") -> tuple[tuple[str, ...], tuple[str, ...]]:
    norm = _norm(f"{title} {text}")
    families = []
    reasons = []
    for family, (_weight, phrases) in MULTILINGUAL_NARRATIVE_FAMILIES.items():
        matched = next((phrase for phrase in phrases if phrase in norm), None)
        if matched:
            families.append(family)
            reasons.append(f"multilingual:{family}:{matched}")
    return tuple(dict.fromkeys(families)), tuple(reasons)


def multilingual_source_profile(source_title: str, source_groups=()) -> bool:
    groups = {str(item) for item in (source_groups or ())}
    if groups & FOREIGN_INFLUENCE_GROUPS:
        return True
    source = _norm(source_title)
    return any(hint in source for hint in SOURCE_PROFILE_HINTS)


def spread_score(spread_count: int, unique_sources: int, cross_platform: bool = False) -> int:
    # Repetition inside one source is useful metadata, but it is not the same as
    # cross-source amplification. The score is therefore driven by independent
    # sources, with cross-platform spread as an extra signal.
    n = max(1, int(unique_sources or 1))
    score = 0
    if n >= 2:
        score += 2
    if n >= 3:
        score += 2
    if n >= 5:
        score += 2
    if n >= 10:
        score += 2
    if n >= 20:
        score += 1
    if cross_platform and n >= 2:
        score += 2
    return min(score, 10)


def deterministic_audit_sample(text: str) -> bool:
    digest = hashlib.sha256((text or "").encode("utf-8", "ignore")).digest()
    return int.from_bytes(digest[:2], "big") % 50 == 0


def _weekly_hits(n, terms): return [x for x in terms if x in n]
def _weekly_near(n, anchors, signals, window=260):
    for a in anchors:
        pos=n.find(a)
        while pos>=0:
            seg=n[max(0,pos-window):pos+len(a)+window]
            hit=[x for x in signals if x in seg]
            if hit: return a,hit,seg
            pos=n.find(a,pos+1)
    return '',[],''
def weekly_relevance_signals(text,title='',source_title='',source_kind=''):
    n=_norm(f'{title} {text}')
    out=[]
    ua=_weekly_hits(n,('украин','україн','киев','київ','всу','зсу'))
    combat=_weekly_hits(n,('всу','зсу','фронт','днр','лнр','харьков','харків','запорож','запоріж','добропол','краматор','славян','константинов','сумск','сумськ','зона сво'))
    ru_force=_weekly_hits(n,('вс рф','российские войска','российская армия','армия россии','минобороны россии','группировка войск','войска россии'))
    peace=_weekly_hits(n,('мирн','переговор','урегулирован','перемир','прекращени огня'))
    peace_terms=_weekly_hits(n,('первопричин','демилитариз','денацифик','нейтральн статус','внеблок','безъядер','конституц 1996','конституции 1996'))
    if ua and peace_terms and (peace or any(x in n for x in ('конституц 1996','конституции 1996','нейтральн статус'))): out.append(('peace_conditions',6,peace_terms[:3]))
    response=_weekly_hits(n,('законн цел','законной цел','военн цел','двойн назначен','военн логист','удар возмезд','ответн удар','удар в ответ','живой щит','гуманитарн прикрыт'))
    if ua and response: out.append(('justification_response',6,response[:3]))
    ba,bh,bs=_weekly_near(n,('буч',),('инсцен','фальсифик','постанов','провокац','фейк'),320)
    if ba and bh: out.append(('justification_bucha',6,['буча',*bh[:2]]))
    if ua and _weekly_hits(n,('террористическ государств','государственн террор','киевский террор','террористическ режим')): out.append(('justification_terror',6,['terror']))
    ua_mil=bool(ua and (combat or _weekly_hits(n,('военн','армия','командован','резерв','наемник'))))
    ca,ch,cs=_weekly_near(n,('драпат','вивальд'),('потер','окруж','котел','котёл','авантюр','последн резерв','нехват','дефицит','бросил','латан','дыр','разгром','кризис'),320)
    if ua_mil and ca and ch: out.append(('discredit_command',6,[ca,*ch[:3]]))
    merc=_weekly_hits(n,('наемник','иностранн военнослуж','иностранн бойц','колумбийц','иностранц'))
    if ua_mil and merc and _weekly_hits(n,('нехват','дефицит','личн состав','резерв','последн')): out.append(('discredit_reserves',5,merc[:3]))
    pa,ph,ps=_weekly_near(n,('пво укра','украинск пво','пво всу','patriot','патриот','радиолокац','радар'),('нехват','дефицит','уничтожен','поражен','слаб','нечем','истощ','последн'),260)
    if ua and pa and ph and any(x in ps for x in ('украин','всу','зсу','киев','київ')): out.append(('discredit_pvo',5,[pa,*ph[:2]]))
    f16=_weekly_hits(n,('f-16','f16'))
    f16_readiness=_weekly_hits(n,('запчаст','боеприпас','боєприпас','нехват','дефицит','небоеспособ','прикован','техобслуж','ремонт'))
    if ua and f16 and f16_readiness: out.append(('discredit_f16_readiness',5,(f16+f16_readiness)[:4]))
    tg=telegram_source_profile(source_title) if str(source_kind).lower()=='telegram' else ''
    resistance=_weekly_hits(n,('#єднайся','#озброюйся','#захищайся','#гострисокир','#гостривил','#давинагаз','#мусоратцк','#тцкпідараси','#гарнийтцкдохлийтцк'))
    if tg=='pseudo_ua' and resistance: out.append(('destab_resistance',7,resistance[:4]))
    if ua and _weekly_hits(n,('новый майдан','нового майдана','новий майдан','3 октября','3 жовтня')) and _weekly_hits(n,('митинг','протест','майдан','отставк')): out.append(('destab_protest',6,['protest']))
    if _weekly_hits(n,('мобилиз','мобіліз')) and _weekly_hits(n,('женщ','жінок','молод','18-лет','18 лет')) and ua: out.append(('destab_mobilization',5,['mobilization']))
    if _weekly_hits(n,('черная зима','чёрная зима','чорна зима','экономагеддон','економагеддон')): out.append(('destab_winter',6,['winter']))
    donor=_weekly_hits(n,('польш','британ','лондон','франц','макрон','финлянд','стубб','канад','герман','словац','фицо','венгр','евросоюз','ес '))
    support=_weekly_hits(n,('военн помощ','военной помощ','вооружен','оружи','ракет','перехват','радар','starlink','патриот','patriot','финансирован','поддержк','поставк'))
    if ua and donor and support: out.append(('obstruction_support',5,(donor+support)[:4]))
    membership_reject=_weekly_hits(n,('не должна стать членом','не должен стать членом','не должна вступить','никогда не должна стать членом','ніколи не повинна стати членом','never become a member','should never join'))
    eu_nato=_weekly_hits(n,('евросоюз','євросоюз','нато','nato','european union'))
    if ua and membership_reject and eu_nato: out.append(('obstruction_membership_rejection',5,(membership_reject+eu_nato)[:4]))
    if 'рябков' in n and _weekly_hits(n,('британ','лондон')) and _weekly_hits(n,('де-юре','де юре','де-факто','де факто','участник конфликт')): out.append(('obstruction_escalation',6,['ryabkov_uk']))
    if 'euroclear' in n and _weekly_hits(n,('актив','средств','деньг')) and _weekly_hits(n,('росс','украин','киев','конфискац','изъят','краж','украден')): out.append(('obstruction_assets',6,['euroclear']))
    if ua and _weekly_hits(n,('patriot','патриот')) and _weekly_hits(n,('пакет','дефицит','не включ','нет ракет','не хват','перехват')): out.append(('obstruction_patriot',6,['patriot']))
    if ua and 'starlink' in n and _weekly_hits(n,('удар','дальн','покрыт','разреш','росси')): out.append(('obstruction_starlink',6,['starlink']))
    if ua and 'mol' in n and _weekly_hits(n,('венгр','хранилищ','нефтепродукт')): out.append(('obstruction_hungary',5,['mol']))
    if ua and _weekly_hits(n,('не наша война','это не наша война')) and _weekly_hits(n,('польш','варшав')): out.append(('obstruction_protest',6,['poland_protest']))
    success=_weekly_hits(n,('взят под контроль','взяли под контроль','освобож','захвачен','зачист','окруж','котел','котёл','продвинул','продвиж','уничтож','поразил','поражен','сжига','разгром','прорыв'))
    if (combat or ua) and success and (ru_force or _weekly_hits(n,('российск','вс рф','минобороны'))): out.append(('military_success',6,(success+combat)[:4]))
    if ua and _weekly_hits(n,('два массирован','2 массирован')) and _weekly_hits(n,('семь групповых','7 групповых')): out.append(('military_weekly_strikes',7,['2+7 strikes']))
    if ua and 'геран' in n and _weekly_hits(n,('нет средств','не имеет средств','неэффектив','противодейств','нечем','не способны','не способн')): out.append(('military_tech_advantage',6,['geran']))
    infra=_weekly_hits(n,('интернет','связь','зв’язок','дата-центр','датацентр','электроэнерг','електроенерг','отоплен','опален','топлив','палив','газоснабж','водоснабж'))
    stress=_weekly_hits(n,('угроз','перебо','отключ','відключ','дефицит','дефіцит','потер','втрат','поврежд','пошкодж','кризис','криза','не будет','не буде'))
    if ua and infra and stress: out.append(('destab_infrastructure',5,(infra+stress)[:4]))
    econ=_weekly_hits(n,('ндс','налог','подат','бюджет','инфляц','інфляц','тариф','цены','ціни','долг','борг','зарплат','пенси','безработ'))
    econ_stress=_weekly_hits(n,('повыс','підвищ','рост','зрост','дефицит','дефіцит','дыра','кризис','криза','не хватает','не вистач','сокращ','урез'))
    if ua and econ and econ_stress: out.append(('destab_economic_pressure',5,(econ+econ_stress)[:4]))
    geran_geo=_weekly_hits(n,('одесс','одес','харьков','харків','запорож','запоріж','днепр','дніпр','сумск','сумськ'))
    if (ua or geran_geo) and 'геран' in n and _weekly_hits(n,('скорост','швидк','дальност','дальність','реактивн','технолог','800 км')): out.append(('military_tech_advantage',5,['geran_tech']))
    media=_weekly_hits(n,('сми','змі','пресса','press','times','bbc','reuters'))
    media_attack=_weekly_hits(n,('пропаганд','фейк','лож','лж','врет','бреш','замалчив','замовчу','русофоб'))
    if _weekly_hits(n,('росси','рф','украин','україн')) and media and media_attack: out.append(('media_discrediting',5,(media+media_attack)[:4]))
    rear=_weekly_hits(n,('производ','виробниц','цех','завод','склад','логист','логіст','железнодорож','залізнич','порт','аэродром','аеродром','дата-центр','телеком','бпла','дрон'))
    rear_damage=_weekly_hits(n,('поврежд','пошкодж','уничтож','знищ','приостанов','призупин','останов','зупин','поражен','удар'))
    if ua and rear and rear_damage: out.append(('discredit_rear_capacity',5,(rear+rear_damage)[:4]))
    peace_context=_weekly_hits(n,('папа','ватикан','святого престол','святой престол','посреднич','медиатор','гуманитарн мисси','мирн инициатив'))
    if ua and peace and peace_context: out.append(('peace_context',4,peace_context[:3]))
    best=max([x[1] for x in out],default=0)
    return best,out

def classify(
    text: str,
    *,
    title: str = "",
    source_title: str = "",
    source_kind: str = "",
    spread_count: int = 1,
    unique_sources: int = 1,
    cross_platform: bool = False,
    dynamic_terms: tuple[str, ...] = (),
    source_groups=(),
) -> PrefilterResult:
    text = text or ""
    junk = primary_dedup.quick_junk_reason(text)
    hashtags = extract_hashtags(text)
    if junk:
        return PrefilterResult(
            "DROP_HARD", junk, 0, 0, spread_score(spread_count, unique_sources, cross_platform),
            (), (), hashtags, ("hard_junk",),
        )

    actors = actor_speaker_hits(text, title, source_title)
    mentions = actor_mention_hits(text, title)
    priority_speakers = tuple(actor for actor in actors if actor in CORE_ACTORS_ALWAYS)
    secondary_speakers = tuple(actor for actor in actors if actor not in CORE_ACTORS_ALWAYS)
    mention_only = tuple(actor for actor in mentions if actor not in actors)
    official_originators = official_actor_originator_hits(
        source_title, text, title, mention_only
    )
    dscore, domain_hits = domain_score(text, title)
    signal_text = _strip_source_boilerplate(text, source_title)
    signal_title = _strip_source_boilerplate(title, source_title)
    iscore, families, influence_reasons = influence_signals(signal_text, signal_title)
    issue_families, issue_reasons = issue_claim_signals(signal_text, signal_title)
    new_issue_families = tuple(f for f in issue_families if f not in families)
    if new_issue_families:
        iscore = min(20, iscore + sum(ISSUE_CLAIM_RULES[f][0] for f in new_issue_families))
        families = tuple(dict.fromkeys((*families, *new_issue_families)))
    multilingual_reasons = ()
    if multilingual_source_profile(source_title, source_groups):
        ml_families, multilingual_reasons = multilingual_influence_signals(signal_text, signal_title)
        new_ml_families = tuple(f for f in ml_families if f not in families)
        if new_ml_families:
            iscore = min(20, iscore + sum(MULTILINGUAL_NARRATIVE_FAMILIES[f][0] for f in new_ml_families))
            families = tuple(dict.fromkeys((*families, *new_ml_families)))
    strong_hashtags = tuple(
        tag for tag in hashtags
        if any(stem in tag for stem in HASHTAG_SIGNAL_STEMS)
    )
    if strong_hashtags:
        iscore = min(20, iscore + min(4, len(strong_hashtags) * 2))
        families = tuple(dict.fromkeys((*families, "strong_hashtag")))
    channel_score, channel_hits = channel_signal_score(source_title, text)
    if channel_score:
        iscore = min(20, iscore + channel_score)
        families = tuple(dict.fromkeys((*families, "channel_lexicon")))
    source_prior, source_prior_label = source_influence_prior(source_title)
    relay_network = relay_network_label(source_title)
    if source_prior:
        iscore = min(20, iscore + source_prior)
    tg_profile = telegram_source_profile(source_title) if str(source_kind or "").lower() == "telegram" else ""
    tg_score, tg_families, tg_reasons = telegram_recall_signals(text, title, source_title, source_kind)
    if tg_score:
        iscore = min(20, iscore + tg_score)
        families = tuple(dict.fromkeys((*families, *tg_families)))
    weekly_score, weekly_signals = weekly_relevance_signals(text, title, source_title, source_kind)
    dynamic_terms = tuple(dict.fromkeys(term for term in dynamic_terms if term))[:12]
    if dynamic_terms:
        # Auto-harvested channel language is a safety net only. It is recorded
        # as a signal, but does not increase influence_score and therefore
        # cannot by itself satisfy any ANALYZE threshold.
        families = tuple(dict.fromkeys((*families, "dynamic_channel_lexicon")))
    sscore = spread_score(spread_count, unique_sources, cross_platform)
    reasons = list(influence_reasons)
    if issue_reasons:
        reasons.extend(issue_reasons)
    if multilingual_reasons:
        reasons.extend(multilingual_reasons)
        reasons.append("multilingual_source_profile")
    if strong_hashtags:
        reasons.append("strong_hashtags:" + ",".join(strong_hashtags[:8]))
    if channel_hits:
        reasons.append("channel_lexicon:" + ",".join(channel_hits[:8]))
    if source_prior:
        reasons.append(f"source_prior:{source_prior_label}:{source_prior}")
    if tg_reasons:
        reasons.extend(tg_reasons)
    if weekly_signals:
        for family, _score, hits in weekly_signals:
            reasons.append("weekly_relevance:" + family + (":" + ",".join(str(x) for x in hits[:4]) if hits else ""))
    if relay_network:
        reasons.append(f"relay_network:{relay_network}")
    if dynamic_terms:
        reasons.append("dynamic_terms:" + ",".join(dynamic_terms[:8]))
    if domain_hits:
        reasons.append("domain:" + ",".join(domain_hits[:12]))
    if hashtags:
        reasons.append("hashtags:" + ",".join(hashtags[:12]))
    if secondary_speakers:
        reasons.append("secondary_speaker:" + ",".join(secondary_speakers))
    if mention_only:
        reasons.append("actor_mentions:" + ",".join(mention_only))
    if official_originators:
        reasons.append("official_originator:" + ",".join(official_originators))

    protocol_low_value = low_value_protocol(text, title)
    if protocol_low_value:
        reasons.append("protocol_low_value")

    if (priority_speakers or official_originators) and protocol_low_value and dscore <= 3 and iscore <= 1 and sscore < 6:
        route = "HOLD"
        held = tuple(dict.fromkeys((*priority_speakers, *official_originators)))
        reasons.append("watchlist_protocol_hold:" + ",".join(held))
    elif priority_speakers:
        route = "MUST_ANALYZE"
        reasons.append("watchlist_speaker:" + ",".join(priority_speakers))
    elif official_originators:
        route = "MUST_ANALYZE"
        reasons.append("official_originator_select:" + ",".join(official_originators))
    elif weekly_score >= 5:
        route = "ANALYZE"
        reasons.append("weekly_relevance_upgrade")
    elif tg_profile == "pseudo_ua" and tg_score >= 4:
        route = "ANALYZE"
        reasons.append("telegram_pseudo_ua_recall")
    elif tg_profile in {"official_military", "military_amplifier"} and "military_success_claim" in tg_families:
        route = "ANALYZE"
        reasons.append("telegram_military_claim_recall")
    elif tg_profile in {"official_diplomatic", "official_political"} and (
        tg_score >= 5 or (tg_score >= 4 and (dscore >= 3 or any(a in CORE_ACTORS_ALWAYS for a in mention_only)))
    ):
        route = "ANALYZE"
        reasons.append("telegram_official_framing_recall")
    elif tg_profile == "factcheck_amplifier" and tg_score >= 5:
        route = "ANALYZE"
        reasons.append("telegram_factcheck_recall")
    elif tg_profile == "amplifier" and tg_score >= 5:
        route = "ANALYZE"
        reasons.append("telegram_amplifier_framing_recall")
    elif source_prior >= 2 and dscore >= 3:
        route = "ANALYZE"
        reasons.append("source_profile_plus_domain")
    elif iscore >= 8:
        route = "ANALYZE"
        reasons.append("strong_influence_signals")
    elif dscore >= 3 and iscore >= 5:
        route = "ANALYZE"
        reasons.append("strong_targeted_narrative")
    elif dscore >= 8 and iscore >= 3:
        route = "ANALYZE"
        reasons.append("strong_domain_plus_signal")
    elif dscore >= 6 and iscore >= 4:
        route = "ANALYZE"
        reasons.append("domain_plus_influence")
    elif sscore >= 6 and (dscore >= 3 or iscore >= 2):
        route = "ANALYZE"
        reasons.append("spread_upgrade")
    elif relay_network and iscore == 0 and sscore < 3 and weekly_score < 5 and not dynamic_terms and not tg_score:
        route = "DROP_SOFT"
        reasons.append("relay_low_signal_drop")
    elif weekly_score >= 4:
        route = "HOLD"
        reasons.append("weekly_context_hold")
    elif dynamic_terms:
        route = "HOLD"
        reasons.append("dynamic_lexicon_hold")
    elif tg_profile in {"official_diplomatic", "official_political"} and any(a in CORE_ACTORS_ALWAYS for a in mention_only):
        route = "HOLD"
        reasons.append("official_actor_event_hold")
    elif tg_score:
        route = "HOLD"
        reasons.append("telegram_profile_hold")
    elif dscore >= 3 or iscore >= 2 or sscore >= 3:
        route = "HOLD"
        reasons.append("hold_for_context_or_spread")
    else:
        route = "HOLD" if deterministic_audit_sample(text) else "DROP_SOFT"
        reasons.append("audit_sample" if route == "HOLD" else "low_signal")

    return PrefilterResult(
        route, "", dscore, iscore, sscore, actors, families, hashtags, tuple(reasons)
    )

