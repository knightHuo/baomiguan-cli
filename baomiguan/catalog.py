"""Discover annual activities from the official, paginated course catalog."""

import re
from datetime import datetime, timedelta, timezone

from .client import ApiError


CHINA_TZ = timezone(timedelta(hours=8))


class Catalog:
    PATH = "/portal/main-api/spc/cms/publish/listByCat.do"
    PAGE_SIZE = 20
    MAX_PAGES = 50

    def __init__(self, client):
        self.client = client

    @staticmethod
    def _date(value):
        if value is None or value == "":
            return None
        try:
            if isinstance(value, (int, float)) or str(value).isdigit():
                number = float(value)
                if abs(number) >= 100_000_000_000:
                    number /= 1000
                return datetime.fromtimestamp(number, CHINA_TZ)
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return parsed.replace(tzinfo=CHINA_TZ) if parsed.tzinfo is None else parsed.astimezone(CHINA_TZ)
        except (ValueError, TypeError, OverflowError, OSError):
            return None

    @staticmethod
    def _year(title):
        years = set(re.findall(r"(?<!\d)(20\d{2})(?!\d)", title))
        return int(next(iter(years))) if len(years) == 1 else None

    def _course(self, item):
        course_id = str(item.get("SYS_UUID") or "").strip()
        title = str(item.get("name") or "").strip()
        if not course_id or not title:
            raise ApiError("课程目录缺少课程标识或名称，需更新适配")
        start, end = self._date(item.get("startValidDate")), self._date(item.get("endValidDate"))
        now = datetime.now(CHINA_TZ)
        if start and end and end < start:
            status = "unknown"
        elif start and now < start:
            status = "upcoming"
        elif end and now > end:
            status = "ended"
        elif start and end:
            status = "active"
        else:
            status = "unknown"
        from urllib.parse import urlencode

        return {
            "id": course_id, "title": title, "year": self._year(title),
            "startDate": start.isoformat() if start else None,
            "endDate": end.isoformat() if end else None,
            "status": status,
            "url": "https://www.baomi.org.cn/bmCourseDetail/info?" + urlencode({"id": course_id}),
        }

    def list_courses(self):
        courses, known_ids, seen_pages = [], set(), set()
        for page in range(self.MAX_PAGES):
            response = self.client.get(self.PATH, params={
                "module": "pc-listbycat-course", "siteId": "95",
                "resourceType": "COURSE_PACKET",
                "sorts": "COURSE_PACKET_courseStatus desc, pub_created desc",
                "pageNo": page, "pageSize": self.PAGE_SIZE,
                "courseType": 0, "courseSource": 1,
            })
            if response.get("success") not in (True, "true"):
                raise ApiError("官方课程目录读取失败，请稍后重试")
            items = response.get("result")
            if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                raise ApiError("官方课程目录结构变化，需要更新适配")
            fingerprint = tuple(str(item.get("SYS_UUID")) for item in items)
            if items and fingerprint in seen_pages:
                raise ApiError("官方目录重复返回同一页，已停止以避免循环")
            seen_pages.add(fingerprint)
            for item in items:
                course = self._course(item)
                if course["id"] not in known_ids:
                    known_ids.add(course["id"])
                    courses.append(course)
            has_next = response.get("hasNext")
            if has_next is False or has_next == "false" or has_next == 0:
                return courses
            try:
                total = int(response["totalCount"]) if "totalCount" in response else None
                pages = int(response["totalPages"]) if "totalPages" in response else None
            except (ValueError, TypeError):
                total = pages = None
            if total is not None and len(known_ids) >= total:
                return courses
            if pages is not None and page + 1 >= pages:
                return courses
            if has_next is None and len(items) < self.PAGE_SIZE:
                return courses
            if not items:
                raise ApiError("官方目录分页状态不一致，未能读取完整列表")
        raise ApiError("官方目录超过安全分页上限，请缩小范围或更新适配")

    def resolve(self, year, courses=None):
        try:
            selected_year = int(year)
        except (TypeError, ValueError) as exc:
            raise ApiError("请选择有效的年度") from exc
        if not 2000 <= selected_year <= 2099:
            raise ApiError("请选择有效的年度")
        available = self.list_courses() if courses is None else courses
        return [course for course in available if course.get("year") == selected_year]
