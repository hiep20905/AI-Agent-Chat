from datetime import date

from test_agent_pipeline import DATASET, FakeModel, FakeSemanticLayer, _answers

from text_to_bi.superset_ai_agent.modules.chat.services import pipeline
from text_to_bi.superset_ai_agent.modules.chat.services.retrieval import (
    MAX_LISTED_METRICS,
    curated_examples,
    listed_metrics,
    ranked_metrics,
    similar_examples,
)
from text_to_bi.superset_ai_agent.modules.semantic_layer.services.catalog import SemanticCatalog


def _metric(metric_id, name, code, definition="", unit=None, term_ids=()):
    return {
        "dataset_id": 12, "metric_id": metric_id, "name": name, "metric_name": code, "definition": definition,
        "unit": unit, "glossary_term_ids": list(term_ids),
    }


WEATHER_METRICS = [
    _metric(16, "Nhiệt độ trung bình", "nhiet_do_trung_binh", "Nhiệt độ không khí trung bình (°C).", "°C"),
    _metric(17, "Nhiệt độ cao nhất", "nhiet_do_cao_nhat", "Nhiệt độ cao nhất của từng ngày.", "°C"),
    _metric(20, "Tổng lượng mưa", "tong_luong_mua", "Tổng lượng mưa (mm) của từng ngày.", "mm"),
    _metric(23, "Tốc độ gió tối đa", "toc_do_gio_toi_da", "Tốc độ gió tối đa (km/h) của từng ngày.", "km/h"),
    _metric(19, "Độ ẩm trung bình", "do_am_trung_binh", "Độ ẩm tương đối trung bình (%).", "%", term_ids=(5,)),
]
TERMS = [{"id": 5, "name": "Độ ẩm", "synonyms": ["độ ướt không khí"], "definition": "Hơi nước trong không khí."}]


def _names(items):
    return [item["metric"]["name"] if "metric" in item else item["name"] for item in items]


def test_the_metric_named_whole_comes_first_and_the_longer_name_wins():
    ranked = ranked_metrics("Nhiệt độ cao nhất ở Huế tuần này", WEATHER_METRICS)

    assert _names(ranked)[:2] == ["Nhiệt độ cao nhất", "Nhiệt độ trung bình"]
    assert ranked[0]["matched"] == ["“nhiet do cao nhat”"]


def test_words_in_a_metric_name_count_more_than_words_only_in_its_definition():
    days = _metric(21, "Số ngày có mưa", "so_ngay_co_mua", "Số ngày có lượng mưa lớn hơn 0 mm.", "ngày")

    ranked = ranked_metrics("Lượng mưa hôm qua ở Cần Thơ", [days, *WEATHER_METRICS])

    assert _names(ranked)[:2] == ["Tổng lượng mưa", "Số ngày có mưa"]


def test_a_glossary_synonym_finds_the_metric_its_term_links_to():
    ranked = ranked_metrics("Độ ướt không khí ở Đà Lạt hôm qua", WEATHER_METRICS, TERMS)

    assert ranked[0]["metric"]["name"] == "Độ ẩm trung bình" and "“do uot khong khi”" in ranked[0]["matched"]


def test_a_small_catalog_is_listed_whole_best_match_first_with_what_matched():
    listed = listed_metrics("Tổng lượng mưa ở Cần Thơ", WEATHER_METRICS)

    assert len(listed) == len(WEATHER_METRICS) and listed[0]["name"] == "Tổng lượng mưa"
    assert listed[0]["match"] and all("match" not in item for item in listed if item["name"].startswith("Nhiệt"))


def test_a_large_catalog_keeps_its_best_matches_and_what_the_turn_before_queried():
    many = WEATHER_METRICS + [
        _metric(100 + index, f"Chỉ số vận hành {index}", f"chi_so_van_hanh_{index}")
        for index in range(MAX_LISTED_METRICS)
    ]

    listed = listed_metrics("Tổng lượng mưa ở Cần Thơ", many, kept_keys=[(12, 23)])

    assert _names(listed) == ["Tổng lượng mưa", "Tốc độ gió tối đa"]


RAIN = [{"dataset_id": 12, "metric_id": 20, "name": "Mưa"}]
ANSWERED = [
    {"question": "Tổng lượng mưa ở Cần Thơ tuần này", "metrics": RAIN},
    {"question": "Tổng lượng mưa ở Cần Thơ tuần trước", "metrics": RAIN},
    {"question": "Nhiệt độ cao nhất ở Huế", "metrics": [{"dataset_id": 12, "metric_id": 17, "name": "Nóng"}]},
    {"question": "Tổng lượng mưa ở Huế", "metrics": [{"dataset_id": 99, "metric_id": 1, "name": "Mưa riêng"}]},
]


def test_a_question_asked_before_is_an_example_once_whatever_its_period():
    examples = similar_examples("Tổng lượng mưa ở Cần Thơ hôm qua", ANSWERED, {(12, 20), (12, 17)})

    assert [item["question"] for item in examples] == ["Tổng lượng mưa ở Cần Thơ tuần này"]
    assert examples[0]["similarity"] == 1.0


def test_an_example_on_a_metric_the_asker_cannot_query_or_about_something_else_is_left_out():
    examples = similar_examples("Tổng lượng mưa ở Huế", ANSWERED, {(12, 20), (12, 17)})

    assert [item["question"] for item in examples] == ["Tổng lượng mưa ở Cần Thơ tuần này"]
    assert similar_examples("Có bao nhiêu thành phố?", ANSWERED, {(12, 20), (12, 17)}) == []


def test_an_answered_example_keeps_its_metrics_and_filters_but_not_its_period_or_rows():
    query_result = {
        "queries": [{"dataset_id": 12, "metric_id": 20, "period": None}],
        "metrics": [{"label": "Tổng lượng mưa"}],
        "dimensions": [{"name": "ngay"}],
        "filters": [
            {"column": "ngay", "operator": ">=", "value": "2026-10-19", "source": "time"},
            {"column": "thanh_pho_key", "operator": "==", "value": "7", "source": "place"},
        ],
        "rows": [{"ngay": "2026-10-19", "tong_luong_mua": 3.2}],
    }

    example = pipeline.answered_example(
        "Tổng lượng mưa ở Cần Thơ tuần này", [{"dataset_id": 12, "metric_id": 20, "name": "Tổng lượng mưa"}],
        query_result,
    )

    assert example == {
        "question": "Tổng lượng mưa ở Cần Thơ tuần này",
        "source": "câu đã trả lời",
        "metrics": [{"dataset_id": 12, "metric_id": 20, "name": "Tổng lượng mưa"}],
        "dimensions": ["ngay"],
        "filters": [{"column": "thanh_pho_key", "operator": "==", "value": "7", "source": "place"}],
    }


def test_the_understanding_step_sees_the_ranked_metrics_and_the_closest_answered_question():
    catalog = SemanticCatalog(metrics=WEATHER_METRICS, datasets=[DATASET])
    model = FakeModel(_answers(term_ids=(), metrics=((12, 17),), rule_ids=(), question_kind="quantitative"))
    answered = [{
        "question": "Nhiệt độ cao nhất ở Hà Nội tuần này",
        "metrics": [{"dataset_id": 12, "metric_id": 17, "name": "Nhiệt độ cao nhất"}],
        "dimensions": ["ngay"], "filters": [],
    }]

    run = pipeline.run_agent(
        "Nhiệt độ cao nhất ở Hà Nội hôm qua", [], model, lambda: catalog, run_metric=FakeSemanticLayer(),
        today=date(2026, 10, 26), answered=answered,
    )
    events = list(run)

    prompt = model.prompt("Understanding")
    assert prompt.index('"Nhiệt độ cao nhất"') < prompt.index('"Tổng lượng mưa"') and '"match"' in prompt
    assert "Câu hỏi tương tự đã trả lời" in prompt and "Nhiệt độ cao nhất ở Hà Nội tuần này" in prompt
    started = next(data for name, data in events if name == "tool_start" and data["name"] == "understand_question")
    assert started["arguments"]["retrieved"]["examples"] == [{"question": "Nhiệt độ cao nhất ở Hà Nội tuần này",
                                                          "similarity": 1.0, "source": None}]
    assert started["arguments"]["retrieved"]["selected"] == "Nhiệt độ cao nhất"


def test_a_metric_named_whole_and_longer_than_any_other_match_is_picked_by_retrieval():
    listed = listed_metrics("Nhiệt độ cao nhất ở Hà Nội hôm qua", WEATHER_METRICS)

    assert listed[0]["name"] == "Nhiệt độ cao nhất" and listed[0]["confident"] is True
    assert not any(item.get("confident") for item in listed[1:])


def test_no_metric_is_picked_when_none_is_named_whole():
    assert not any(item.get("confident") for item in listed_metrics("Gió giật ở Đà Nẵng hôm nay", WEATHER_METRICS))
    assert not any(item.get("confident") for item in listed_metrics("Nhiệt độ ở Huế", WEATHER_METRICS))


def test_an_administrators_example_on_an_expert_rule_is_an_faq_entry_and_wins_a_tie():
    rules = [{"id": 1, "name": "Ngày mưa lớn", "dataset_id": 12, "metric_ids": [20],
              "example_questions": ["Tuần này Cần Thơ có ngày mưa lớn nào không?"]},
             {"id": 2, "name": "Không ví dụ", "dataset_id": 12, "metric_ids": [20], "example_questions": []}]
    curated = curated_examples(rules, WEATHER_METRICS)
    answered = [{"question": "Tuần trước Cần Thơ có ngày mưa lớn nào không?", "metrics": RAIN,
                 "source": "câu đã trả lời"}]

    examples = similar_examples("Tháng này Cần Thơ có ngày mưa lớn nào không?", [*curated, *answered], {(12, 20)})

    assert curated == [{
        "question": "Tuần này Cần Thơ có ngày mưa lớn nào không?",
        "metrics": [{"dataset_id": 12, "metric_id": 20, "name": "Tổng lượng mưa"}],
        "expert_rule": "Ngày mưa lớn", "source": "quản trị viên soạn",
    }]
    assert [item["source"] for item in examples] == ["quản trị viên soạn"]
