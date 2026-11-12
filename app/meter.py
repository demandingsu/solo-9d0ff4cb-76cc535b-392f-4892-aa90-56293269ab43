"""抄表站分页抓取与月度峰谷用量还原。

数据模型（页面结构见 ../meter-site）：
- 每页一张 table[data-meter-records]，每行 tr[data-record-id] 携带
  data-meter-id / data-reading-time / data-event / data-peak-kwh / data-valley-kwh
- 分页沿用报价站约定：nav[data-pagination] 内 a[rel=next]

还原规则：
1. 每月以月初与下月月初两次抄表读数划界，用量 = 月末累计 - 月初累计。
2. 月中换表（同一时刻出现旧表 replace_stop 与新表 replace_start）时，
   该月用量 = 旧表（止码 - 月初码）+ 新表（月末码 - 起码），两侧增量相加。
3. 同表同刻的相同记录只计一次（跨页重复）；同刻读数冲突则该读数两侧月份均不可用。
4. 月界/换表记录缺失、同刻冲突、累计值倒退 -> 受影响月份标记不可用，绝不填零。
5. 任一分页抓取失败 -> 记录失败地址，且整份用量标记为不可比价。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from .fetcher import FetchError, fetch_url, join_url
from .html_dom import parse_html

MAX_METER_PAGES = 50  # 分页安全上限，防止异常站点造成无限翻页

# 月份不可用原因码
REASON_FETCH_FAILED = "fetch_failed"            # 分页抓取失败，记录可能缺失
REASON_MISSING_BOUNDARY = "missing_boundary"    # 月界或换表记录缺失
REASON_CONFLICT = "conflicting_reading"         # 同表同刻读数冲突
REASON_REGRESSION = "cumulative_regression"     # 同表累计值倒退

# 页面行事件类型
EVENT_REPLACE_START = "replace_start"
EVENT_REPLACE_STOP = "replace_stop"


@dataclass(frozen=True)
class MeterRecord:
    """一条抄表记录（累计读数，单位 kWh）。"""
    record_id: str
    meter_id: str
    reading_time: date
    event: str | None  # routine | replace_start | replace_stop | None
    peak_kwh: Decimal | None
    valley_kwh: Decimal | None
    source_url: str


@dataclass
class MeterSegment:
    """一只表在某段时间（通常为一个月，换表时为月内片段）的读数划界。"""
    meter_id: str
    start_time: date
    end_time: date
    peak_start: Decimal | None
    peak_end: Decimal | None
    valley_start: Decimal | None
    valley_end: Decimal | None
    peak_start_record_id: str | None
    peak_end_record_id: str | None
    valley_start_record_id: str | None
    valley_end_record_id: str | None


@dataclass
class MonthReconstruction:
    month: int
    available: bool
    peak_kwh: Decimal | None
    valley_kwh: Decimal | None
    reasons: list[str]
    segments: list[MeterSegment]
    record_ids: list[str]


@dataclass
class MeterCrawlResult:
    records: list[MeterRecord] = field(default_factory=list)
    failures: list = field(default_factory=list)          # list[CrawlFailure]
    visited_urls: list[str] = field(default_factory=list)
    duplicate_record_ids: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #
def _parse_decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    value = value.strip()
    if value == "":
        return None
    try:
        return Decimal(value)
    except Exception:
        return None


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    value = value.strip()
    # 页面统一使用 ISO 日期；容忍带时间的写法
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        try:
            return datetime.fromisoformat(value).date()
        except ValueError:
            return None


def parse_meter_page(markup: str, page_url: str) -> tuple[list[MeterRecord], str | None]:
    """解析一页抄表记录，并返回下一页地址（无则 None）。"""
    root = parse_html(markup)
    table = root.find(tag="table", attr="data-meter-records")
    records: list[MeterRecord] = []
    if table is not None:
        for row in table.find_all(tag="tr", attr="data-record-id"):
            record_id = row.get("data-record-id") or ""
            meter_id = row.get("data-meter-id") or ""
            reading_time = _parse_date(row.get("data-reading-time"))
            if not record_id or not meter_id or reading_time is None:
                # 关键标识缺失的行无法参与还原，跳过而非臆造
                continue
            event = (row.get("data-event") or "").strip() or None
            records.append(
                MeterRecord(
                    record_id=record_id,
                    meter_id=meter_id,
                    reading_time=reading_time,
                    event=event,
                    peak_kwh=_parse_decimal(row.get("data-peak-kwh")),
                    valley_kwh=_parse_decimal(row.get("data-valley-kwh")),
                    source_url=page_url,
                )
            )
    next_link = root.find(tag="a", attr="rel", attr_value="next")
    next_url = (
        join_url(page_url, next_link.get("href"))
        if next_link is not None and next_link.get("href")
        else None
    )
    return records, next_url


def crawl_meter_site(index_url: str, timeout: float = 8.0) -> MeterCrawlResult:
    """从抄表目录首页开始跟随 next 分页抓取，失败即停止翻页并登记地址。"""
    # 延迟导入，避免与报价站抓取模块循环依赖
    from .scraper import CrawlFailure

    result = MeterCrawlResult()
    seen: set[str] = set()
    current: str | None = index_url

    while current and current not in seen and len(seen) < MAX_METER_PAGES:
        seen.add(current)
        result.visited_urls.append(current)
        try:
            page = fetch_url(current, timeout=timeout)
        except FetchError as exc:
            result.failures.append(
                CrawlFailure(url=current, stage="meter_list", reason=exc.reason, status=exc.status)
            )
            break
        page_records, next_url = parse_meter_page(page.text, page.url)
        result.records.extend(page_records)
        current = next_url

    result.duplicate_record_ids = _dedup_identical_records(result.records)
    return result


# --------------------------------------------------------------------------- #
# 去重与冲突识别
# --------------------------------------------------------------------------- #
def _dedup_identical_records(records: list[MeterRecord]) -> list[str]:
    """同表同刻且峰谷读数完全相同的记录只保留第一条，返回被丢弃记录的 id。

    同表同刻但读数不同属于冲突，不在此处理，由还原阶段标记两侧月份不可用。
    判定基于整条记录（峰+谷），跨页整行重复只计一次。
    """
    seen_rows: set[tuple] = set()
    dropped: list[str] = []
    for record in records:
        signature = (
            record.meter_id,
            record.reading_time,
            record.peak_kwh,
            record.valley_kwh,
        )
        if signature in seen_rows:
            dropped.append(record.record_id)
        else:
            seen_rows.add(signature)
    return dropped


# --------------------------------------------------------------------------- #
# 还原
# --------------------------------------------------------------------------- #
@dataclass
class _Reading:
    value: Decimal | None
    record_id: str
    valid: bool  # False 表示同刻冲突，该读数不可用


def _build_reading_index(
    records: list[MeterRecord],
) -> tuple[
    dict[tuple[str, date, str], _Reading],
    dict[str, date | None],
    dict[str, date],
]:
    """构建 (电表, 时刻, 段别) -> 读数 索引，以及换表起止时刻索引。"""
    index: dict[tuple[str, date, str], _Reading] = {}
    replace_start: dict[str, date | None] = {}
    replace_stop: dict[str, date] = {}

    for record in records:
        if record.event == EVENT_REPLACE_START:
            replace_start.setdefault(record.meter_id, record.reading_time)
        if record.event == EVENT_REPLACE_STOP:
            # 同一表理论上只有一次止码；保留最早一次
            replace_stop[record.meter_id] = min(
                replace_stop.get(record.meter_id, record.reading_time),
                record.reading_time,
            )
        for kind, value in (("peak", record.peak_kwh), ("valley", record.valley_kwh)):
            if value is None:
                continue
            key = (record.meter_id, record.reading_time, kind)
            if key not in index:
                index[key] = _Reading(value=value, record_id=record.record_id, valid=True)
            elif index[key].value != value:
                # 同表同刻读数冲突：两条记录均作废
                index[key].valid = False

    return index, replace_start, replace_stop


def _meter_alive_at(
    meter_id: str,
    when: date,
    first_seen: dict[str, date],
    replace_start: dict[str, date | None],
    replace_stop: dict[str, date],
) -> bool:
    """该电表在指定时刻是否应当存在读数。

    - 新表（有 replace_start）在起码时刻起在装，起码之前不参与划界；
    - 旧表（有 replace_stop）在止码时刻止仍在装（止码是其最后一次读数），止码之后退出。
    """
    start = replace_start.get(meter_id)
    if start is not None and when < start:
        return False  # 尚未装表
    if first_seen.get(meter_id, when) > when:
        return False
    stop = replace_stop.get(meter_id)
    return stop is None or when <= stop


def _segment_for_meter(
    meter_id: str,
    start: date,
    end: date,
    index: dict[tuple[str, date, str], _Reading],
) -> MeterSegment:
    def pick(kind: str, when: date) -> tuple[Decimal | None, str | None]:
        reading = index.get((meter_id, when, kind))
        if reading is None:
            return None, None
        return reading.value, reading.record_id

    peak_start, peak_start_rid = pick("peak", start)
    peak_end, peak_end_rid = pick("peak", end)
    valley_start, valley_start_rid = pick("valley", start)
    valley_end, valley_end_rid = pick("valley", end)
    return MeterSegment(
        meter_id=meter_id,
        start_time=start,
        end_time=end,
        peak_start=peak_start,
        peak_end=peak_end,
        valley_start=valley_start,
        valley_end=valley_end,
        peak_start_record_id=peak_start_rid,
        peak_end_record_id=peak_end_rid,
        valley_start_record_id=valley_start_rid,
        valley_end_record_id=valley_end_rid,
    )


def _segment_problems(segment: MeterSegment, index) -> list[str]:
    """检查单表片段的峰/谷读数：缺失、冲突、倒退。"""
    problems: list[str] = []
    for kind in ("peak", "valley"):
        start_value = getattr(segment, f"{kind}_start")
        end_value = getattr(segment, f"{kind}_end")
        start_reading = index.get((segment.meter_id, segment.start_time, kind))
        end_reading = index.get((segment.meter_id, segment.end_time, kind))
        if start_value is None or end_value is None:
            problems.append(REASON_MISSING_BOUNDARY)
            continue
        if not (start_reading.valid and end_reading.valid):
            problems.append(REASON_CONFLICT)
            continue
        if end_value < start_value:
            problems.append(REASON_REGRESSION)
    return problems


def reconstruct_year(
    records: list[MeterRecord],
    year: int,
    fetch_failed: bool = False,
) -> list[MonthReconstruction]:
    """按时间与电表编号，把累计抄表读数还原为目标年份 12 个月的峰谷用量。"""
    index, replace_start, replace_stop = _build_reading_index(records)
    first_seen: dict[str, date] = {}
    for record in records:
        first_seen[record.meter_id] = min(
            first_seen.get(record.meter_id, record.reading_time),
            record.reading_time,
        )

    months: list[MonthReconstruction] = []
    for month in range(1, 13):
        start = date(year, month, 1)
        end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)

        # 在月界应当有读数的全部电表（月中换表时可能不止一只）
        meter_ids = sorted(
            meter_id
            for meter_id in first_seen
            if _meter_alive_at(meter_id, start, first_seen, replace_start, replace_stop)
            or _meter_alive_at(meter_id, end, first_seen, replace_start, replace_stop)
        )

        reasons: list[str] = []
        if fetch_failed:
            # 分页失败后无法确认后续页面是否含本月读数，一律不可用且不填零
            reasons.append(REASON_FETCH_FAILED)
        if not meter_ids:
            # 该月没有任何在装电表的读数可用于划界
            reasons.append(REASON_MISSING_BOUNDARY)

        segments: list[MeterSegment] = []
        for meter_id in meter_ids:
            stop = replace_stop.get(meter_id)
            r_start = replace_start.get(meter_id)
            seg_start = start
            seg_end = end
            # 片段裁剪：换表时刻落在 [start, end] 月界闭区间内时也要收窄片段。
            # 旧表止码恰好在本月月初 -> 片段退化为零长度，后续读数缺失检查会判其不可用；
            # 新表起码恰好在下月月初 -> 同理，不会把它误算进本月。
            if stop is not None and start <= stop < end:
                # 旧表在月中（或本月月初）拆走：片段截止于止码时刻
                seg_end = stop
            if r_start is not None and start < r_start <= end:
                # 新表在月中（或下月月初）装上：片段起始于起码时刻
                seg_start = r_start
            if seg_start >= seg_end:
                # 零长度片段（如换表恰好发生在月界，该表本月无实际运行区间）：跳过
                continue
            segment = _segment_for_meter(meter_id, seg_start, seg_end, index)
            for problem in _segment_problems(segment, index):
                if problem not in reasons:
                    reasons.append(problem)
            segments.append(segment)

        used_record_ids = sorted(
            {
                rid
                for segment in segments
                for rid in (
                    segment.peak_start_record_id,
                    segment.peak_end_record_id,
                    segment.valley_start_record_id,
                    segment.valley_end_record_id,
                )
                if rid
            }
        )

        if reasons:
            months.append(
                MonthReconstruction(
                    month=month,
                    available=False,
                    peak_kwh=None,
                    valley_kwh=None,
                    reasons=reasons,
                    segments=segments,
                    record_ids=used_record_ids,
                )
            )
            continue

        peak_total = sum(
            (seg.peak_end - seg.peak_start for seg in segments), Decimal("0")
        )
        valley_total = sum(
            (seg.valley_end - seg.valley_start for seg in segments), Decimal("0")
        )
        months.append(
            MonthReconstruction(
                month=month,
                available=True,
                peak_kwh=peak_total,
                valley_kwh=valley_total,
                reasons=[],
                segments=segments,
                record_ids=used_record_ids,
            )
        )

    return months
