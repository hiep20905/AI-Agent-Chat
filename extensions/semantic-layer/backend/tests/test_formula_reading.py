import pytest

from text_to_bi.superset_semantic_layer.modules.semantic_query.services.contracts import parse_contract_metric
from text_to_bi.superset_semantic_layer.modules.semantic_query.services.formula_reading import (
    FormulaVocabulary,
    RowKindGrain,
    read_formula,
)

LABELS = {
    "loai_quan_sat": "Loại quan sát",
    "tong_luong_mua_mm": "Tổng lượng mưa (mm)",
    "nhiet_do_c": "Nhiệt độ (°C)",
    "trang_thai": "Trạng thái",
}
WITHOUT_GRAIN = FormulaVocabulary(column_labels=LABELS)
WITH_GRAIN = FormulaVocabulary(
    column_labels=LABELS,
    row_kind_column="loai_quan_sat",
    grains=(RowKindGrain("DU_BAO_NGAY", "day", "Dự báo theo ngày"), RowKindGrain("DU_BAO_GIO", "hour", None)),
)
DAILY = "loai_quan_sat = 'DU_BAO_NGAY'"
RAINY_DAY_SHARE = (
    f"100.0 * COUNT(CASE WHEN {DAILY} AND tong_luong_mua_mm > 0 THEN 1 END)"
    f" / NULLIF(COUNT(CASE WHEN {DAILY} THEN 1 END), 0)"
)


def _read(expression, vocabulary):
    return read_formula(parse_contract_metric(expression), vocabulary)


def test_a_ratio_moves_the_shared_condition_aside_and_counts_rows():
    assert _read(RAINY_DAY_SHARE, WITHOUT_GRAIN) == {
        "scale": "100",
        "numerator": "số dòng có Tổng lượng mưa (mm) lớn hơn 0",
        "denominator": "số dòng",
        "only_rows": ["Loại quan sát là “DU_BAO_NGAY”"],
    }


def test_a_row_kind_with_a_grain_counts_days_and_names_its_rows():
    assert _read(RAINY_DAY_SHARE, WITH_GRAIN) == {
        "scale": "100",
        "numerator": "số ngày có Tổng lượng mưa (mm) lớn hơn 0",
        "denominator": "số ngày",
        "only_rows": ["Loại quan sát là “DU_BAO_NGAY” (Dự báo theo ngày)"],
    }


@pytest.mark.parametrize(
    ("expression", "vocabulary", "numerator", "only_rows"),
    [
        (
            f"COUNT(CASE WHEN {DAILY} AND tong_luong_mua_mm > 0 THEN 1 END)",
            WITHOUT_GRAIN,
            "số dòng có Loại quan sát là “DU_BAO_NGAY” và Tổng lượng mưa (mm) lớn hơn 0",
            [],
        ),
        (
            f"COUNT(CASE WHEN {DAILY} AND tong_luong_mua_mm > 0 THEN 1 END)",
            WITH_GRAIN,
            "số ngày có Tổng lượng mưa (mm) lớn hơn 0",
            ["Loại quan sát là “DU_BAO_NGAY” (Dự báo theo ngày)"],
        ),
        (
            "AVG(CASE WHEN loai_quan_sat = 'DU_BAO_GIO' THEN nhiet_do_c END)",
            WITH_GRAIN,
            "trung bình của Nhiệt độ (°C)",
            ["Loại quan sát là “DU_BAO_GIO”"],
        ),
        (
            "MAX(CASE WHEN trang_thai IN ('A', 'B') AND NOT nhiet_do_c IS NULL THEN nhiet_do_c END)",
            WITHOUT_GRAIN,
            "giá trị lớn nhất của Nhiệt độ (°C)",
            ["Trạng thái thuộc “A”, “B”", "Nhiệt độ (°C) có giá trị"],
        ),
        ("SUM(cot_chua_co_nhan)", WITHOUT_GRAIN, "tổng của cot_chua_co_nhan", []),
    ],
)
def test_plain_metrics_read_in_business_words(expression, vocabulary, numerator, only_rows):
    reading = _read(expression, vocabulary)
    assert (reading["numerator"], reading["only_rows"], reading["denominator"]) == (numerator, only_rows, None)


def test_a_dataset_with_one_grain_counts_its_rows_in_that_unit():
    vocabulary = FormulaVocabulary(column_labels=LABELS, grains=(RowKindGrain(None, "hour"),))
    assert _read("COUNT(CASE WHEN tong_luong_mua_mm > 0 THEN 1 END)", vocabulary)["numerator"] == (
        "số giờ có Tổng lượng mưa (mm) lớn hơn 0"
    )
