"""Course workflows adapted from NB-XX/AutoBaomiGuan (MIT).

Only platform-confirmed state is reported as complete. Mutating requests are
issued once; a transport failure leaves their outcome explicitly unknown.
"""

from __future__ import annotations

import math
import json
import time
import uuid
from typing import Any, Callable

from .client import ApiError


API = "/portal/main-api/v2/"
Emit = Callable[[str], None]
Cancel = Callable[[], bool]


class WorkflowError(ValueError):
    """The platform response cannot safely support the requested operation."""


class _Cancelled(Exception):
    pass


def _noop(_message: str) -> None:
    pass


def _never() -> bool:
    return False


def _check_cancel(cancel: Cancel) -> None:
    if cancel():
        raise _Cancelled()


def _list(data: Any) -> list[dict]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("listdata", "list", "rows", "records"):
            if isinstance(data.get(key), list):
                return _list(data[key])
    return []


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(str(value).rstrip("%"))
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _duration(value: Any) -> int:
    if isinstance(value, str) and ":" in value:
        parts = value.split(":")
        if len(parts) not in (2, 3) or not all(part.isdigit() for part in parts):
            return 0
        seconds = 0
        for part in parts:
            seconds = seconds * 60 + int(part)
        return seconds
    number = _number(value)
    return int(number) if number is not None and number > 0 else 0


def _finished(data: Any, duration: int = 0) -> bool:
    if not isinstance(data, dict):
        return False
    for key in ("isFinish", "finishStatus", "finished", "completeStatus"):
        if data.get(key) is True or str(data.get(key)).lower() in ("1", "true"):
            return True
    studied = _number(data.get("studyLength", data.get("studyTime")))
    total = _number(data.get("resourceLength", data.get("timeLength"))) or duration
    return bool(total > 0 and studied is not None and studied >= total)


def _progress_finished(data: Any, known_total: int) -> bool:
    if not isinstance(data, dict) or known_total <= 0:
        return False
    if _finished({key: data[key] for key in
                  ("isFinish", "finishStatus", "finished", "completeStatus") if key in data}):
        return True
    # The current official course page displays progressRate * 100 percent.
    rate = _number(data.get("progressRate"))
    if rate == 1:
        return True
    for key in ("studyProgress", "courseProgress", "progress", "percent", "percentage"):
        value = _number(data.get(key))
        if value is not None and value >= 100:
            return True
    for done_key, total_key in (("finishedCount", "totalCount"),
                                ("finishCount", "resourceCount"),
                                ("completedCount", "totalCount"),
                                ("studyCount", "courseCount")):
        done, total = _number(data.get(done_key)), _number(data.get(total_key))
        if done is not None and total is not None and total > 0 and done >= total:
            return True
    return False


class CourseService:
    def __init__(self, client, *, sleep: Callable[[float], None] = time.sleep):
        self.client = client
        self._sleep = sleep

    def _confirm(self, read, confirmed, cancel: Cancel, emit: Emit):
        """Confirm a mutation with up to three reads, never repeat the mutation."""
        latest = None
        for attempt in range(3):
            _check_cancel(cancel)
            try:
                latest = read()
            except ApiError:
                if attempt == 2:
                    raise
            else:
                if confirmed(latest):
                    return latest
            if attempt < 2:
                _check_cancel(cancel)
                emit(f"平台结果尚未确认，1 秒后只读重查（{attempt + 2}/3）")
                self._sleep(1)
        return latest

    def _error(self, exc: Exception) -> str:
        text = str(exc) or type(exc).__name__
        token = getattr(self.client, "token", None)
        return text.replace(token, "[已隐藏]") if isinstance(token, str) and token else text

    def _params(self, **values) -> dict:
        token = getattr(self.client, "token", None)
        return {**values, **({"token": token} if token else {})}

    @staticmethod
    def _response(response: Any, *, writing: bool = False) -> dict:
        if not isinstance(response, dict):
            raise WorkflowError("平台返回格式异常")
        status = response.get("status")
        rejected = (status is not None and str(status) != "0")
        rejected = rejected or response.get("successResult") is False
        result = response.get("result")
        rejected = rejected or result is False
        rejected = rejected or (isinstance(result, (int, float, str)) and result in (0, "0"))
        if rejected:
            raise WorkflowError(str(response.get("message") or response.get("msg") or "平台未接受请求"))
        if writing and str(status) != "0":
            raise WorkflowError("平台未明确确认请求成功，请先核对平台记录")
        return response

    def _read(self, endpoint: str, params: dict, cancel: Cancel = _never) -> Any:
        _check_cancel(cancel)
        response = self._response(self.client.get(API + endpoint, params=params))
        if "data" not in response:
            raise WorkflowError("平台响应缺少 data 字段")
        return response["data"]

    def _directory(self, course_id: str, cancel: Cancel = _never) -> list[dict]:
        data = self._read("coursePacket/getCourseDirectoryList",
                          self._params(coursePacketId=course_id, scale=1), cancel)
        entries = []

        def visit(items, parents=()):
            for item in _list(items):
                title = str(item.get("name") or item.get("title") or "未命名章节")
                children = item.get("subDirectory") or item.get("children")
                if _list(children):
                    visit(children, (*parents, title))
                    continue
                directory_id = item.get("SYS_UUID") or item.get("id") or item.get("directoryId")
                if not directory_id:
                    raise WorkflowError(f"章节“{title}”缺少目录标识")
                entries.append({"id": str(directory_id), "title": " / ".join((*parents, title))})

        visit(data)
        if not entries:
            raise WorkflowError("没有获取到可识别的课程目录")
        return entries

    def _resources(self, course_id: str, directory_id: str, cancel: Cancel) -> list[dict]:
        data = self._read("coursePacket/getCourseResourceList",
                          self._params(coursePacketId=course_id, directoryId=directory_id), cancel)
        resources = []
        for item in _list(data):
            resource_id = item.get("SYS_UUID") or item.get("resourceDirectoryId") or item.get("id")
            if not resource_id:
                raise WorkflowError("课程资源缺少目录标识")
            resources.append({"id": str(resource_id),
                              "title": str(item.get("name") or item.get("resourceName") or "未命名课程"),
                              "duration": _duration(item.get("timeLength", item.get("resourceLength"))),
                              "raw": item})
        if not resources:
            raise WorkflowError("没有获取到课程资源")
        return resources

    def _status(self, course_id: str, resource: dict, cancel: Cancel) -> Any:
        return self._read("coursePacket/getResourceUserStatistic",
                          self._params(coursePacketId=course_id, resourceDirectoryId=resource["id"]), cancel)

    def _progress(self, course_id: str, cancel: Cancel = _never) -> dict:
        progress = self._read("coursePacket/getCourseUserStatistic",
                              self._params(coursePacketId=course_id), cancel)
        if not isinstance(progress, dict):
            raise WorkflowError("课程进度返回格式异常")
        return progress

    def progress(self, course_id: str) -> dict:
        """Read the current platform progress without enumerating every lesson."""
        return self._progress(course_id)

    def details(self, course_id: str) -> dict:
        course = self._read("coursePacket/getCoursePacket", self._params(coursePacketId=course_id))
        if not isinstance(course, dict):
            raise WorkflowError("课程详情返回格式异常")
        sections = self._directory(course_id)
        for section in sections:
            section["resources"] = self._resources(course_id, section["id"], _never)
            for resource in section["resources"]:
                resource["completed"] = _finished(self._status(course_id, resource, _never), resource["duration"])
        return {"course": course, "sections": sections, "progress": self._progress(course_id)}

    def study(self, course_id: str, emit: Emit = _noop, cancel: Cancel = _never) -> dict:
        summary = {"success": False, "total": 0, "completed": 0, "skipped": 0,
                   "failed": 0, "errors": [], "verified": False, "cancelled": False}
        try:
            emit("正在读取课程目录")
            sections = self._directory(course_id, cancel)
            for section in sections:
                try:
                    resources = self._resources(course_id, section["id"], cancel)
                except _Cancelled:
                    raise
                except Exception as exc:
                    summary["errors"].append(f"{section['title']}：{self._error(exc)}")
                    continue
                summary["total"] += len(resources)
                for resource in resources:
                    try:
                        _check_cancel(cancel)
                        emit(f"正在核对：{resource['title']}")
                        if _finished(self._status(course_id, resource, cancel), resource["duration"]):
                            summary["skipped"] += 1
                            emit(f"平台已确认完成，跳过：{resource['title']}")
                            continue
                        if resource["duration"] <= 0 or not resource["raw"].get("resourceID"):
                            raise WorkflowError("资源缺少有效时长或 resourceID，已停止该项提交")
                        _check_cancel(cancel)
                        params = self._params(courseId=course_id, resourceId=resource["raw"]["resourceID"],
                            resourceDirectoryId=resource["id"], resourceLength=resource["duration"],
                            studyLength=resource["duration"], studyTime=resource["duration"],
                            startTime=int(time.time() * 1000), resourceName=resource["title"],
                            resourceType="1", resourceLibId="3")
                        emit(f"正在提交：{resource['title']}")
                        try:
                            response = self.client.get(API + "studyTime/saveCoursePackage.do", params=params)
                        except ApiError as exc:
                            raise WorkflowError("提交结果未知，请先核对平台进度；本次不会自动重试。" + self._error(exc)) from exc
                        self._response(response, writing=True)
                        status = self._confirm(lambda: self._status(course_id, resource, cancel),
                            lambda data: _finished(data, resource["duration"]), cancel, emit)
                        if not _finished(status, resource["duration"]):
                            raise WorkflowError("提交已受理，平台尚未确认该课程完成")
                        summary["completed"] += 1
                    except _Cancelled:
                        raise
                    except Exception as exc:
                        summary["failed"] += 1
                        summary["errors"].append(f"{resource['title']}：{self._error(exc)}")
            _check_cancel(cancel)
            emit("正在重新核对平台总进度")
            summary["progress"] = self._confirm(lambda: self._progress(course_id, cancel),
                lambda progress: _progress_finished(progress, summary["total"]), cancel, emit)
            summary["verified"] = _progress_finished(summary["progress"], summary["total"])
            summary["success"] = bool(summary["verified"] and not summary["errors"])
            if not summary["verified"]:
                summary["errors"].append("平台总进度尚未明确确认全部完成，请查看课程进度")
        except _Cancelled:
            summary["cancelled"] = True
            summary["errors"].append("已停止后续请求；已发出的请求请以平台记录为准")
        except Exception as exc:
            summary["errors"].append(self._error(exc))
        if summary["cancelled"]:
            summary["message"] = "已停止后续请求，已发出的请求请以平台记录为准"
        elif summary["success"]:
            summary["message"] = f"平台已确认课程完成，本次完成 {summary['completed']} 项，跳过 {summary['skipped']} 项"
        else:
            summary["message"] = "本次处理尚未全部完成，请查看各项结果与平台进度"
        return summary

    def _exam_id(self, course_id: str, cancel: Cancel) -> str:
        data = self._read("coursePacket/getCourseRelateExam", self._params(coursePacketId=course_id), cancel)
        items = _list(data) or ([data] if isinstance(data, dict) and data.get("examId") else [])
        ids = {str(item["examId"]) for item in items if item.get("examId")}
        if not ids:
            raise WorkflowError("当前课程没有可识别的关联考试，未使用旧年度考试代替")
        if len(ids) != 1:
            raise WorkflowError("当前课程关联了多个考试，需要先明确考试范围")
        return ids.pop()

    def _exam_result(self, exam_id: str, cancel: Cancel) -> dict | None:
        data = self._read("activity/exam/getExamResultMaxScore.do", self._params(examId=exam_id), cancel)
        if not data:
            return None
        if not isinstance(data, dict):
            raise WorkflowError("考试成绩返回格式异常")
        return data

    @staticmethod
    def _new_result(before: dict | None, after: dict) -> bool:
        if not before:
            return True
        old_count, new_count = _number(before.get("answerCount")), _number(after.get("answerCount"))
        if old_count is not None and new_count is not None and new_count > old_count:
            return True
        old_date, new_date = _number(before.get("submit_date")), _number(after.get("submit_date"))
        if old_date is not None and new_date is not None and new_date > old_date:
            return True
        old_score, new_score = _number(before.get("score")), _number(after.get("score"))
        return bool(old_score is not None and new_score is not None and new_score > old_score)

    def exam(self, course_id: str, emit: Emit = _noop, cancel: Cancel = _never,
             *, allow_retake: bool = True) -> dict:
        result = {"success": False, "score": None, "pending": False, "submitted": False,
                  "cancelled": False, "message": ""}
        try:
            emit("正在查找当前课程关联的考试")
            exam_id = self._exam_id(course_id, cancel)
            result["exam_id"] = exam_id
            before = self._exam_result(exam_id, cancel)
            previous_score = _number(before.get("score")) if before else None
            if not allow_retake and previous_score is not None and previous_score >= 0:
                displayed_score = int(previous_score) if previous_score.is_integer() else previous_score
                result.update(existing=True, score=displayed_score,
                    message=f"平台已有考试记录，最高成绩为 {displayed_score} 分；本次未取卷或提交，请明确选择是否重考")
                return result
            emit("正在获取当前试卷")
            paper = self._read("activity/exam/getExamContentData.do",
                               {"examId": exam_id, "randomId": uuid.uuid4().hex}, cancel)
            if not isinstance(paper, dict) or not paper.get("randomId"):
                raise WorkflowError("试卷缺少有效的请求标识，未提交")
            groups = paper.get("typeList")
            if not isinstance(groups, list) or not groups or any(not isinstance(group, dict) for group in groups):
                raise WorkflowError("试卷题型列表格式异常或为空，未提交")
            answers = []
            for group in groups:
                questions = group.get("questionList")
                if (not isinstance(questions, list) or not questions
                        or any(not isinstance(question, dict) for question in questions)):
                    raise WorkflowError("试卷题目列表格式异常或为空，未提交")
                for question in questions:
                    if ("answer" not in question or question["answer"] in (None, "", [])
                            or not question.get("id") or not question.get("tqId")):
                        raise WorkflowError("题目缺少必要字段或答案，未猜测或提交")
                    answer = question["answer"]
                    answers.append({"parentId": "0", "qstId": question["id"], "resultFlag": 0,
                        "standardAnswer": answer, "subCount": 0, "tqId": question["tqId"],
                        "userAnswer": answer, "userScoreRate": "100%", "viewTypeId": group.get("type", 1)})
            if not answers:
                raise WorkflowError("试卷没有可提交的题目")
            _check_cancel(cancel)
            emit("正在提交试卷；本次提交不会自动重试")
            try:
                response = self.client.post(API + "activity/exam/saveExamResultJc.do", json={
                    "examId": exam_id, "examResult": json.dumps(answers, ensure_ascii=False),
                    "randomId": paper["randomId"], "startDate": time.strftime("%Y-%m-%d %H:%M:%S")})
            except ApiError as exc:
                result["uncertain"] = True
                raise WorkflowError("提交结果未知，请先查询平台考试记录，避免重复提交。" + self._error(exc)) from exc
            self._response(response, writing=True)
            result["submitted"] = True
            emit("正在查询平台成绩")
            def confirmed_score(data):
                score = _number(data.get("score")) if data else None
                return score is not None and score >= 0 and self._new_result(before, data)

            after = self._confirm(lambda: self._exam_result(exam_id, cancel), confirmed_score, cancel, emit)
            score = _number(after.get("score")) if after else None
            if score is None or score < 0 or not self._new_result(before, after):
                result.update(pending=True, message="试卷已提交，最高成绩接口尚不能确认本次结果；请在保密观官网查看考试记录，勿通过再次考试刷新")
                return result
            result["score"] = int(score) if score.is_integer() else score
            _check_cancel(cancel)
            emit("正在同步平台已确认的实际成绩")
            try:
                response = self.client.get(API + "studyTime/updateCoursePackageExamInfo.do", params=self._params(
                    courseId=course_id, orgId="", isExam=1, isCertificate=0, examResult=result["score"]))
            except ApiError as exc:
                result["uncertain"] = True
                raise WorkflowError("成绩已生成，但课程状态同步结果未知，请核对平台记录。" + self._error(exc)) from exc
            self._response(response, writing=True)
            result.update(success=True, message=f"考试已提交，平台最高成绩为 {result['score']} 分，课程状态已同步")
        except _Cancelled:
            result.update(cancelled=True, message="已停止后续请求；已提交内容请以平台记录为准")
        except Exception as exc:
            result["message"] = self._error(exc)
        return result
