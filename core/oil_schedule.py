"""One source for adjustment dates and countdowns across all image types."""
from datetime import date

CACHE_KEY = "next_adjustment_date"


def parse_date(raw) -> date | None:
    text = str(raw or "").strip()
    if len(text) == 8 and text.isascii() and text.isdigit():
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    if len(text) != 10:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


async def next_adjustment(db, today: date, raw=None, *, persist=False) -> dict:
    """Live valid date, then saved live date, then historical event cache.

    Recalculate days against the caller's date on every read; never cache days or
    extrapolate a future date from an expired event. Successful live responses
    are persisted independently of before_change_time, which may be absent.
    """
    candidates = [parse_date(raw)]
    if candidates[0] and candidates[0] >= today:
        if persist and db is not None:
            await db.set_meta(CACHE_KEY, candidates[0].isoformat())
    elif db is not None:
        candidates.extend([
            parse_date(await db.get_meta(CACHE_KEY)),
            parse_date(await db.next_change_date()),
        ])
    future = next((d for d in candidates if d is not None and d >= today), None)
    if future:
        days = (future - today).days
        return {"next_date": future.isoformat(), "days_left": days,
                "days_text": f"距今 {days} 天" + ("（今天）" if days == 0 else ""),
                "status": "known", "message": ""}
    previous = max((d for d in candidates if d is not None), default=None)
    message = (f"已公布调价日期 {previous.isoformat()} 已过，等待上游更新"
               if previous else "下次调价信息暂不可用，请刷新数据")
    return {"next_date": "", "days_left": None, "days_text": "",
            "status": "expired" if previous else "missing", "message": message}
