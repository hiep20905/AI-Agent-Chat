import math
from collections.abc import Sequence
from typing import Any

from ....common.utils.text import normalized_text
from ...schema_link.services.linking import STOPWORDS

# A catalog up to this many metrics is listed whole, best match first; a larger one only lists its best matches, the
# metrics the question's glossary terms name and those the turn before queried.
MAX_LISTED_METRICS = 40
TOP_METRICS = 15
# A metric phrase (its name, code or a linked term's name or synonym) named whole by the question weighs this much per
# word, so "nhiệt độ cao nhất" outranks "nhiệt độ".
PHRASE_WEIGHT = 2.0
# A shared word in a metric's name, code or term counts this many times one only in its definition, so "lượng mưa"
# finds "Tổng lượng mưa" before "Số ngày có mưa", whose definition speaks of "lượng mưa".
NAME_WORD_WEIGHT = 2.0
# How many answered questions are shown as examples, and how close to the asked one they must be (shared words over
# all words, time words left out, since an example's period is never reused).
TOP_EXAMPLES = 3
MIN_EXAMPLE_SIMILARITY = 0.5


def _words(*texts: Any) -> set[str]:
    return {word for text in texts if text for word in normalized_text(text).split() if word not in STOPWORDS}


def _named(phrase: str, question: str) -> bool:
    return bool(phrase) and f" {phrase} " in f" {question} "


def _metric_terms(metric: dict[str, Any], glossary_terms: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    key = {"dataset_id": metric["dataset_id"], "metric_id": metric["metric_id"]}
    linked = set(metric.get("glossary_term_ids") or [])
    return [term for term in glossary_terms if term["id"] in linked or key in (term.get("metrics") or [])]


def _phrases(metric: dict[str, Any], terms: list[dict[str, Any]]) -> list[str]:
    texts = [metric.get("name"), (metric.get("metric_name") or "").replace("_", " ")]
    texts += [text for term in terms for text in (term.get("name"), *(term.get("synonyms") or []))]
    return list(dict.fromkeys(phrase for phrase in map(normalized_text, filter(None, texts)) if phrase))


def ranked_metrics(
    question: str, metrics: Sequence[dict[str, Any]], glossary_terms: Sequence[dict[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Every metric with how well the question names it, best first: `score` and the phrases or words that `matched`.

    A metric is found by its name, its code, the names and synonyms of the glossary terms linked to it, and the words of
    its definition; a word most metrics share ("trung bình") says little, a rare one ("mưa") says more.
    """
    asked = normalized_text(question)
    asked_words = _words(question)
    documents = []
    for metric in metrics:
        terms = _metric_terms(metric, glossary_terms)
        phrases = _phrases(metric, terms)
        named_words = _words(*phrases)
        words = named_words | _words(metric.get("definition"), metric.get("unit"))
        documents.append((metric, phrases, named_words, words))
    counts: dict[str, int] = {}
    for *_named_parts, words in documents:
        for word in words:
            counts[word] = counts.get(word, 0) + 1
    rarity = {word: math.log(1 + len(documents) / count) for word, count in counts.items()}

    ranked = []
    for order, (metric, phrases, named_words, words) in enumerate(documents):
        named = [phrase for phrase in phrases if _named(phrase, asked)]
        shared = sorted(asked_words & words)
        score = sum(rarity[word] * (NAME_WORD_WEIGHT if word in named_words else 1.0) for word in shared)
        if named:
            score += PHRASE_WEIGHT * max(len(phrase.split()) for phrase in named)
        matched = [f"“{phrase}”" for phrase in named] or ([f"từ: {', '.join(shared)}"] if shared else [])
        ranked.append((-round(score, 2), order, {"metric": metric, "score": round(score, 2), "matched": matched}))
    return [item for *_key, item in sorted(ranked, key=lambda entry: entry[:2])]


def listed_metrics(
    question: str,
    metrics: Sequence[dict[str, Any]],
    glossary_terms: Sequence[dict[str, Any]] = (),
    kept_keys: Sequence[tuple[int, int]] = (),
) -> list[dict[str, Any]]:
    """The metrics the understanding step is shown, best match first, each with its `match` when the question names it.

    A catalog of up to MAX_LISTED_METRICS is listed whole, so ranking only orders it; a larger one keeps its TOP_METRICS
    best matches plus the metrics in `kept_keys` (what the turn before queried), so a follow-up naming no metric
    ("còn Huế thì sao?") still finds its own.
    """
    ranked = ranked_metrics(question, metrics, glossary_terms)
    if len(ranked) > MAX_LISTED_METRICS:
        kept = set(kept_keys)
        ranked = [
            item for position, item in enumerate(ranked)
            if (position < TOP_METRICS and item["score"] > 0)
            or (item["metric"]["dataset_id"], item["metric"]["metric_id"]) in kept
        ]
    return [
        {**item["metric"], **({"match": item["matched"]} if item["score"] > 0 else {})}
        for item in ranked
    ]


def similar_examples(
    question: str, answered: Sequence[dict[str, Any]], metric_keys: set[tuple[int, int]],
) -> list[dict[str, Any]]:
    """Questions answered before that ask nearly the same thing, closest first, as examples of the metrics and filters
    that answered them (the FAQ of the dashboard).

    Only an example whose metrics the asker can still query is kept, and one question asked many times, whatever its
    period ("tuần này", "tuần trước"), counts once.
    """
    asked_words = _words(question)
    if not asked_words:
        return []
    scored = []
    seen: set[frozenset[str]] = set()
    for position, example in enumerate(answered):
        words = frozenset(_words(example["question"]))
        keys = {(item["dataset_id"], item["metric_id"]) for item in example.get("metrics") or []}
        if words in seen or not keys or not keys <= metric_keys:
            continue
        seen.add(words)
        similarity = len(asked_words & words) / len(asked_words | words) if words else 0.0
        if similarity >= MIN_EXAMPLE_SIMILARITY:
            scored.append((-similarity, position, {**example, "similarity": round(similarity, 2)}))
    return [item for *_key, item in sorted(scored, key=lambda entry: entry[:2])[:TOP_EXAMPLES]]
