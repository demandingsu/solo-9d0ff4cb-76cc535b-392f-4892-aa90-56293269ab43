"""抄表整理 API 的离线测试（标准库 unittest + FastAPI TestClient）。

夹具：
- tests/fixtures/clean-meter-site/：完整 12 个月（含 6 月中换表与跨页重复），无抓取失败
- meter-site/：随仓库提供的演示站，含冲突、倒退、月界缺失与第 3 页 404

运行（在仓库根目录）：
    python3 -I -m unittest discover -s tests -t . -v
"""
from __future__ import annotations

import sys
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

# -I（隔离模式）会忽略 PYTHONPATH；本地开发环境把第三方依赖装在自定义目录时，
# 显式补入其 site-packages 后再导入 FastAPI（容器/Docker 内依赖在标准路径，此操作为空操作）。
for _candidate in (Path("/tmp/pyuser/lib/python3.11/site-packages"),):
    if _candidate.is_dir() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.meter import (  # noqa: E402
    REASON_CONFLICT,
    REASON_FETCH_FAILED,
    REASON_MISSING_BOUNDARY,
    REASON_REGRESSION,
    MeterRecord,
    crawl_meter_site,
    parse_meter_page,
    reconstruct_year,
)

ROOT = Path(__file__).resolve().parent.parent
CLEAN_URL = (ROOT / "tests" / "fixtures" / "clean-meter-site" / "index.html").as_uri()
DEMO_URL = (ROOT / "meter-site" / "index.html").as_uri()

# 干净夹具还原结果应与比价演示请求的逐月用量一致
EXPECTED_USAGE = {
    1: (260, 360), 2: (240, 340), 3: (280, 380), 4: (300, 400),
    5: (330, 430), 6: (390, 520), 7: (410, 510), 8: (360, 410),
    9: (310, 380), 10: (280, 380), 11: (270, 370), 12: (270, 370),
}


class CrawlParseTest(unittest.TestCase):
    def test_pagination_duplicates_and_failure(self) -> None:
        result = crawl_meter_site(DEMO_URL)
        # 跟随 next 抓到第 3 页（404），失败地址必须登记
        self.assertEqual(len(result.visited_urls), 3)
        self.assertTrue(result.visited_urls[-1].endswith("page-3.html"))
        self.assertEqual(len(result.failures), 1)
        failure = result.failures[0]
        self.assertEqual(failure.stage, "meter_list")
        self.assertEqual(failure.status, 404)
        # 前两页共 14 条记录
        self.assertEqual(len(result.records), 14)
        # R-2025-0015 与 R-2025-0010 同表同刻同读数 -> 只计一次
        self.assertEqual(result.duplicate_record_ids, ["R-2025-0015"])

    def test_clean_fixture_two_pages_without_failure(self) -> None:
        result = crawl_meter_site(CLEAN_URL)
        self.assertEqual(len(result.visited_urls), 2)
        self.assertEqual(result.failures, [])
        self.assertEqual(len(result.duplicate_record_ids), 1)

    def test_parse_skips_malformed_row(self) -> None:
        markup = """
        <table data-meter-records>
          <tr data-record-id="X1" data-meter-id="M1" data-reading-time="2025-01-01"
              data-peak-kwh="10" data-valley-kwh="20"></tr>
          <tr data-record-id="X2" data-meter-id="M1" data-reading-time="not-a-date"
              data-peak-kwh="11" data-valley-kwh="21"></tr>
        </table>
        <a rel="next" href="x.html"></a>
        """
        records, next_url = parse_meter_page(markup, CLEAN_URL)
        self.assertEqual([r.record_id for r in records], ["X1"])
        self.assertTrue(next_url.endswith("x.html"))


class ReconstructionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clean = crawl_meter_site(CLEAN_URL).records

    def test_twelve_months_match_expected_usage(self) -> None:
        months = reconstruct_year(self.clean, 2025)
        self.assertEqual(len(months), 12)
        for item in months:
            peak, valley = EXPECTED_USAGE[item.month]
            self.assertTrue(item.available, f"第 {item.month} 月应可用")
            self.assertEqual(float(item.peak_kwh), float(peak))
            self.assertEqual(float(item.valley_kwh), float(valley))
            self.assertEqual(item.reasons, [])

    def test_mid_month_replacement_sums_both_meter_segments(self) -> None:
        months = {m.month: m for m in reconstruct_year(self.clean, 2025)}
        june = months[6]
        # 6 月由旧表（6/1->6/15）与新表（6/15->7/1）两个片段组成
        self.assertEqual([s.meter_id for s in june.segments], ["M-PEAK-01", "M-PEAK-02"])
        old, new = june.segments
        # 旧表增量 13590-13410=180 峰 / 20150-19910=240 谷
        self.assertEqual(old.peak_end - old.peak_start, Decimal("180"))
        self.assertEqual(old.valley_end - old.valley_start, Decimal("240"))
        # 新表增量 210-0=210 峰 / 280-0=280 谷
        self.assertEqual(new.peak_end - new.peak_start, Decimal("210"))
        self.assertEqual(new.valley_end - new.valley_start, Decimal("280"))
        # 两侧相加：390 / 520
        self.assertEqual(june.peak_kwh, Decimal("390"))
        self.assertEqual(june.valley_kwh, Decimal("520"))
        # 片段记录号分别指向止码/起码记录
        self.assertEqual(old.peak_end_record_id, old.valley_end_record_id)
        self.assertTrue(old.peak_end_record_id.startswith("RC-"))
        self.assertEqual(new.peak_start_record_id, new.valley_start_record_id)

    def test_identical_duplicate_counted_once(self) -> None:
        # 干净夹具含一条跨页重复；还原结果不得因重复翻倍
        months = reconstruct_year(self.clean, 2025)
        august = next(m for m in months if m.month == 8)
        self.assertEqual(august.peak_kwh, Decimal("360"))
        self.assertEqual(august.valley_kwh, Decimal("410"))

    def test_conflicting_reading_marks_both_adjacent_months(self) -> None:
        # 演示站 8/1 同刻冲突 -> 7、8 月不可用
        crawl = crawl_meter_site(DEMO_URL)
        months = {m.month: m for m in reconstruct_year(crawl.records, 2025, False)}
        for month in (7, 8):
            self.assertFalse(months[month].available)
            self.assertIn(REASON_CONFLICT, months[month].reasons)
            self.assertIsNone(months[month].peak_kwh)
            self.assertIsNone(months[month].valley_kwh)

    def test_cumulative_regression_marks_month(self) -> None:
        # 演示站 10/1 累计低于 9/1 -> 9 月倒退不可用
        crawl = crawl_meter_site(DEMO_URL)
        months = {m.month: m for m in reconstruct_year(crawl.records, 2025, False)}
        self.assertFalse(months[9].available)
        self.assertIn(REASON_REGRESSION, months[9].reasons)

    def test_missing_boundary_marks_months(self) -> None:
        # 演示站缺 11/1 月界 -> 10、11、12 月划界不完整
        crawl = crawl_meter_site(DEMO_URL)
        months = {m.month: m for m in reconstruct_year(crawl.records, 2025, False)}
        for month in (10, 11, 12):
            self.assertFalse(months[month].available)
            self.assertIn(REASON_MISSING_BOUNDARY, months[month].reasons)

    def test_fetch_failure_marks_every_month_not_comparable(self) -> None:
        crawl = crawl_meter_site(DEMO_URL)
        months = reconstruct_year(crawl.records, 2025, fetch_failed=True)
        self.assertTrue(all(REASON_FETCH_FAILED in m.reasons for m in months))
        self.assertTrue(all(not m.available for m in months))
        # 不可用月份一律 None，绝不填零
        self.assertTrue(all(m.peak_kwh is None and m.valley_kwh is None for m in months))

    def test_unavailable_never_filled_with_zero(self) -> None:
        # 构造单只表、缺月末读数的场景，验证不会输出 0
        records = [
            MeterRecord("A1", "M9", date(2025, 1, 1), "routine",
                        Decimal("100"), Decimal("200"), "u"),
        ]
        months = reconstruct_year(records, 2025)
        jan = months[0]
        self.assertFalse(jan.available)
        self.assertIsNone(jan.peak_kwh)
        self.assertIsNone(jan.valley_kwh)
        self.assertIn(REASON_MISSING_BOUNDARY, jan.reasons)

    def test_replacement_exactly_on_month_boundary(self) -> None:
        # 换表恰好发生在 2/1 月初：1 月归旧表、2 月归新表，各自单片段
        records = [
            MeterRecord("B1", "OLD", date(2025, 1, 1), "routine", Decimal("0"), Decimal("0"), "u"),
            MeterRecord("B2", "OLD", date(2025, 2, 1), "replace_stop",
                        Decimal("100"), Decimal("200"), "u"),
            MeterRecord("B3", "NEW", date(2025, 2, 1), "replace_start",
                        Decimal("0"), Decimal("0"), "u"),
            MeterRecord("B4", "NEW", date(2025, 3, 1), "routine",
                        Decimal("50"), Decimal("60"), "u"),
        ]
        months = {m.month: m for m in reconstruct_year(records, 2025)}
        jan, feb = months[1], months[2]
        self.assertTrue(jan.available)
        self.assertEqual([s.meter_id for s in jan.segments], ["OLD"])
        self.assertEqual(jan.peak_kwh, Decimal("100"))
        self.assertTrue(feb.available)
        self.assertEqual([s.meter_id for s in feb.segments], ["NEW"])
        self.assertEqual(feb.peak_kwh, Decimal("50"))

    def test_partial_kind_missing_marks_month_unavailable(self) -> None:
        # 峰段读数齐全，但谷段月末读数缺失 -> 整月不可用且不填零
        records = [
            MeterRecord("C1", "M8", date(2025, 1, 1), "routine",
                        Decimal("10"), Decimal("20"), "u"),
            MeterRecord("C2", "M8", date(2025, 2, 1), "routine",
                        Decimal("30"), None, "u"),  # 谷段缺失
        ]
        jan = reconstruct_year(records, 2025)[0]
        self.assertFalse(jan.available)
        self.assertIsNone(jan.peak_kwh)
        self.assertIsNone(jan.valley_kwh)
        self.assertEqual(jan.reasons, [REASON_MISSING_BOUNDARY])

    def test_three_identical_records_counted_once(self) -> None:
        # 同一记录出现三次：只计一次，其余两个记录号都进重复列表
        records = [
            MeterRecord("D1", "M7", date(2025, 1, 1), "routine", Decimal("0"), Decimal("0"), "u"),
            MeterRecord("D2", "M7", date(2025, 1, 1), "routine", Decimal("0"), Decimal("0"), "u"),
            MeterRecord("D3", "M7", date(2025, 1, 1), "routine", Decimal("0"), Decimal("0"), "u"),
            MeterRecord("D4", "M7", date(2025, 2, 1), "routine", Decimal("10"), Decimal("20"), "u"),
        ]
        from app.meter import _dedup_identical_records
        dropped = _dedup_identical_records(records)
        self.assertEqual(sorted(dropped), ["D2", "D3"])
        jan = reconstruct_year(records, 2025)[0]
        self.assertTrue(jan.available)
        self.assertEqual(jan.peak_kwh, Decimal("10"))
        self.assertEqual(jan.valley_kwh, Decimal("20"))

    def test_records_outside_target_year_are_ignored(self) -> None:
        # 2024 年的读数不应参与 2025 年 1 月的在装表判定之外的月份
        records = [
            MeterRecord("E1", "M6", date(2024, 12, 1), "routine",
                        Decimal("0"), Decimal("0"), "u"),
            MeterRecord("E2", "M6", date(2025, 1, 1), "routine",
                        Decimal("10"), Decimal("10"), "u"),
            MeterRecord("E3", "M6", date(2025, 2, 1), "routine",
                        Decimal("20"), Decimal("30"), "u"),
        ]
        months = reconstruct_year(records, 2025)
        jan = months[0]
        self.assertTrue(jan.available)
        self.assertEqual(jan.peak_kwh, Decimal("10"))
        self.assertEqual(jan.valley_kwh, Decimal("20"))


class ApiEndpointTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_clean_site_response_is_comparable_and_ready_for_compare(self) -> None:
        resp = self.client.post("/api/v1/meter-usage", json={"index_url": CLEAN_URL, "year": 2025})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["comparable"])
        self.assertEqual(body["unavailable_months"], [])
        self.assertEqual(body["failures"], [])
        self.assertEqual(len(body["monthly_usage"]), 12)
        # monthly_usage 形状与比价接口输入一致
        first = body["monthly_usage"][0]
        self.assertEqual(set(first), {"month", "peak_kwh", "valley_kwh"})
        self.assertEqual(first, {"month": 1, "peak_kwh": 260.0, "valley_kwh": 360.0})
        # 每月带来源记录与来源页
        jan = body["months"][0]
        self.assertTrue(jan["available"])
        self.assertTrue(jan["source_record_ids"])
        # 来源记录号去重（峰谷来自同一抄表记录时不重复列出）
        self.assertEqual(len(jan["source_record_ids"]), len(set(jan["source_record_ids"])))
        self.assertTrue(all(u.startswith("file://") for u in jan["source_urls"]))
        # 重复记录被登记
        self.assertEqual(len(body["duplicates"]), 1)
        # 6 月换表：两个片段，峰谷各自的起止记录号齐全
        june = body["months"][5]
        self.assertEqual([s["meter_id"] for s in june["segments"]],
                         ["M-PEAK-01", "M-PEAK-02"])
        for segment in june["segments"]:
            self.assertIsNotNone(segment["peak_start_record_id"])
            self.assertIsNotNone(segment["peak_end_record_id"])
            self.assertIsNotNone(segment["valley_start_record_id"])
            self.assertIsNotNone(segment["valley_end_record_id"])

    def test_demo_site_failure_and_anomalies_not_comparable(self) -> None:
        resp = self.client.post("/api/v1/meter-usage", json={"index_url": DEMO_URL, "year": 2025})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        # 存在分页抓取失败 -> 整份用量不得标为可比价
        self.assertFalse(body["comparable"])
        self.assertEqual(body["crawl"]["failure_count"], 1)
        self.assertTrue(body["failures"][0]["url"].endswith("page-3.html"))
        self.assertEqual(body["failures"][0]["status"], 404)
        # 抓取失败后无法确认后续页面是否含缺失读数，12 个月全部不可用，
        # monthly_usage 不得包含任何月份，更不能以零值充数
        self.assertEqual(body["monthly_usage"], [])
        self.assertEqual(body["unavailable_months"], list(range(1, 13)))
        for month in body["months"]:
            self.assertFalse(month["available"])
            self.assertIsNone(month["peak_kwh"])
            self.assertIsNone(month["valley_kwh"])
        reasons = {m["month"]: m["reasons"] for m in body["months"]}
        self.assertIn(REASON_CONFLICT, reasons[7])
        self.assertIn(REASON_REGRESSION, reasons[9])
        self.assertIn(REASON_FETCH_FAILED, reasons[12])

    def test_invalid_scheme_rejected(self) -> None:
        resp = self.client.post(
            "/api/v1/meter-usage", json={"index_url": "ftp://host/index.html", "year": 2025}
        )
        self.assertEqual(resp.status_code, 422)

    def test_missing_index_page_reports_failure(self) -> None:
        missing = (ROOT / "meter-site" / "does-not-exist.html").as_uri()
        resp = self.client.post("/api/v1/meter-usage", json={"index_url": missing, "year": 2025})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertFalse(body["comparable"])
        self.assertEqual(body["failures"][0]["status"], 404)
        self.assertEqual(len(body["monthly_usage"]), 0)

    def test_monthly_usage_feeds_compare_plans(self) -> None:
        # 端到端：抄表整理输出直接作为比价接口的 monthly_usage
        meter_resp = self.client.post(
            "/api/v1/meter-usage", json={"index_url": CLEAN_URL, "year": 2025}
        ).json()
        self.assertTrue(meter_resp["comparable"])
        quote_url = (ROOT / "quote-site" / "index.html").as_uri()
        compare_resp = self.client.post(
            "/api/v1/compare-plans",
            json={
                "list_url": quote_url,
                "region": "上海",
                "monthly_usage": meter_resp["monthly_usage"],
            },
        )
        self.assertEqual(compare_resp.status_code, 200)
        body = compare_resp.json()
        ranked = body["ranked_plans"]
        # P001 费率：峰 0-200@1.05 / 200-400@1.18 / 400+@1.32；谷 0-300@0.42 / 300+@0.55；
        # 月基本费 60。用独立纯算术核算其年度总额并与 API 输出核对。
        def peak_fee(x: float) -> float:
            return min(x, 200) * 1.05 + max(0.0, min(x, 400) - 200) * 1.18 + max(0.0, x - 400) * 1.32

        def valley_fee(x: float) -> float:
            return min(x, 300) * 0.42 + max(0.0, x - 300) * 0.55

        expected_p001 = round(
            sum(
                peak_fee(item["peak_kwh"]) + valley_fee(item["valley_kwh"]) + 60.0
                for item in meter_resp["monthly_usage"]
            ),
            2,
        )
        p001 = next(plan for plan in ranked if plan["plan_id"] == "P001")
        self.assertAlmostEqual(p001["annual_total_cny"], expected_p001, places=2)
        # 排序仍为从低到高，且参与套餐与既有夹具结论一致（P001/P002/P003）
        self.assertEqual([plan["plan_id"] for plan in ranked], ["P003", "P001", "P002"])


if __name__ == "__main__":
    unittest.main()
