from unittest.mock import Mock

import pytest

from baomiguan.catalog import Catalog
from baomiguan.client import ApiError


def item(id="course-2026", name="2026年度全国保密教育线上培训", **extra):
    return {"SYS_UUID": id, "name": name, **extra}


def test_official_catalog_is_paginated_and_ids_are_not_hardcoded():
    client = Mock()
    client.get.side_effect = [
        {"success": True, "result": [item()], "hasNext": True, "totalCount": 2},
        {"success": True, "result": [item("new-2027", "2027年度全国保密教育线上培训")], "hasNext": False},
    ]
    courses = Catalog(client).list_courses()
    assert [course["year"] for course in courses] == [2026, 2027]
    assert [call.kwargs["params"]["pageNo"] for call in client.get.call_args_list] == [0, 1]
    assert courses[1]["id"] == "new-2027"
    assert courses[1]["url"].endswith("id=new-2027")


def test_annual_resolution_does_not_fall_back_and_retains_ambiguity():
    catalog = Catalog(Mock())
    courses = [
        {"id": "old", "year": 2026},
        {"id": "current-a", "year": 2027},
        {"id": "current-b", "year": 2027},
        {"id": "unclassified", "year": None},
    ]
    assert catalog.resolve(2028, courses) == []
    assert len(catalog.resolve("2027", courses)) == 2
    assert catalog.resolve(2026, courses) == [courses[0]]
    catalog.client.get.assert_not_called()


def test_unknown_or_multiple_years_are_not_guessed():
    catalog = Catalog(Mock())
    assert catalog._course(item(name="全国保密教育"))["year"] is None
    assert catalog._course(item(name="2026—2027跨年活动"))["year"] is None
    assert catalog._course(item(name="2027年度培训"))["year"] == 2027


@pytest.mark.parametrize(("start", "end", "status"), [
    ("2000-01-01", "2001-01-01", "ended"),
    ("2098-01-01", "2099-01-01", "upcoming"),
    ("2000-01-01", "2099-01-01", "active"),
    ("2099-01-01", "2000-01-01", "unknown"),
    (None, None, "unknown"),
    ("not-a-date", "invalid", "unknown"),
])
def test_activity_status_and_invalid_dates(start, end, status):
    result = Catalog(Mock())._course(item(startValidDate=start, endValidDate=end))
    assert result["status"] == status


def test_official_millisecond_dates_are_supported():
    result = Catalog(Mock())._course(item(startValidDate=1780588800000, endValidDate=1793375999000))
    assert result["startDate"] == "2026-06-05T00:00:00+08:00"
    assert result["endDate"] == "2026-10-30T23:59:59+08:00"


def test_repeated_page_is_an_error_instead_of_an_infinite_loop():
    client = Mock()
    client.get.return_value = {"success": True, "result": [item()], "hasNext": True}
    with pytest.raises(ApiError, match="重复"):
        Catalog(client).list_courses()
    assert client.get.call_count == 2


def test_pagination_is_bounded():
    client = Mock()
    client.get.side_effect = [
        {"success": True, "result": [item(str(index))], "hasNext": True}
        for index in range(3)
    ]
    catalog = Catalog(client)
    catalog.MAX_PAGES = 3
    with pytest.raises(ApiError, match="上限"):
        catalog.list_courses()
    assert client.get.call_count == 3


@pytest.mark.parametrize("data", [
    {"success": False, "result": []},
    {"success": True, "result": None},
    {"success": True, "result": [None]},
    {"success": True, "result": [{"name": "2026培训"}]},
    {"success": True, "result": [], "hasNext": True},
])
def test_failed_or_changed_catalog_does_not_report_valid_empty_activity(data):
    client = Mock()
    client.get.return_value = data
    with pytest.raises(ApiError):
        Catalog(client).list_courses()


def test_genuinely_empty_catalog_is_valid():
    client = Mock()
    client.get.return_value = {"success": True, "result": [], "hasNext": False, "totalCount": 0}
    assert Catalog(client).list_courses() == []
