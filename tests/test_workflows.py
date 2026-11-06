import copy
import unittest
from collections import defaultdict, deque

from baomiguan.client import ApiError
from baomiguan.workflows import API, CourseService


def response(data):
    return {"status": 0, "data": data}


class FakeClient:
    token = "local-secret-token"

    def __init__(self):
        self.routes = defaultdict(deque)
        self.calls = []

    def add(self, endpoint, *values, method="GET"):
        self.routes[(method, API + endpoint)].extend(values)
        return self

    def _call(self, method, path, payload):
        self.calls.append((method, path, copy.deepcopy(payload)))
        queue = self.routes[(method, path)]
        if not queue:
            raise AssertionError(f"Unexpected {method} {path}")
        value = queue.popleft()
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)

    def get(self, path, params=None):
        return self._call("GET", path, params)

    def post(self, path, json=None):
        return self._call("POST", path, json)

    def mutations(self):
        return [call for call in self.calls if any(endpoint in call[1] for endpoint in
                ("saveCoursePackage.do", "saveExamResultJc.do", "updateCoursePackageExamInfo.do"))]


def study_client(*, statuses=None, progress=None, write=None):
    client = FakeClient()
    client.add("coursePacket/getCourseDirectoryList", response([
        {"name": "第一章", "subDirectory": [{"name": "课程", "SYS_UUID": "directory"}]}]))
    client.add("coursePacket/getCourseResourceList", response({"listdata": [
        {"SYS_UUID": "resource-directory", "resourceID": "video", "name": "示例课程", "timeLength": "00:01:30"}]}))
    client.add("coursePacket/getResourceUserStatistic", *(statuses or [response({"isFinish": True})]))
    progress_response = response(progress if progress is not None else {"progressRate": 1})
    client.add("coursePacket/getCourseUserStatistic", *[progress_response] * 3)
    if write is not None:
        client.add("studyTime/saveCoursePackage.do", write)
    return client


def exam_client(*, before=None, after=None, submit=None, finish=None):
    client = FakeClient()
    client.add("coursePacket/getCourseRelateExam", response([{"examId": "current-exam"}]))
    client.add("activity/exam/getExamResultMaxScore.do", response(before), *[response(after)] * 3)
    client.add("activity/exam/getExamContentData.do", response({
        "randomId": "server-paper-session", "typeList": [{"type": 1, "questionList": [
            {"id": "question", "tqId": "paper-question", "answer": "A"}]}]}))
    client.add("activity/exam/saveExamResultJc.do", response(None) if submit is None else submit, method="POST")
    if finish is not None:
        client.add("studyTime/updateCoursePackageExamInfo.do", finish)
    return client


class CourseWorkflowTests(unittest.TestCase):
    def test_details_normalizes_directory_and_resource(self):
        client = study_client()
        client.add("coursePacket/getCoursePacket", response({"name": "年度培训"}))
        details = CourseService(client, sleep=lambda _: None).details("course")
        resource = details["sections"][0]["resources"][0]
        self.assertEqual(details["sections"][0]["title"], "第一章 / 课程")
        self.assertEqual(resource["duration"], 90)
        self.assertTrue(resource["completed"])
        self.assertEqual(details["progress"], {"progressRate": 1})

    def test_already_finished_course_is_not_resubmitted(self):
        client = study_client()
        result = CourseService(client, sleep=lambda _: None).study("course")
        self.assertTrue(result["success"])
        self.assertEqual((result["total"], result["skipped"], result["completed"]), (1, 1, 0))
        self.assertEqual(client.mutations(), [])

    def test_resource_failure_cannot_be_hidden_by_completed_total_progress(self):
        client = study_client(statuses=[response({"isFinish": False})],
                              write={"status": 2, "message": "不接受该记录"})
        result = CourseService(client, sleep=lambda _: None).study("course")
        self.assertFalse(result["success"])
        self.assertEqual(result["failed"], 1)
        self.assertTrue(result["verified"])
        self.assertIn("不接受", result["errors"][0])

    def test_accepted_write_is_not_equivalent_to_platform_completion(self):
        client = study_client(statuses=[response({"isFinish": False})] * 4,
                              progress={"progressRate": 0}, write=response(None))
        result = CourseService(client, sleep=lambda _: None).study("course")
        self.assertFalse(result["success"])
        self.assertEqual(result["completed"], 0)
        self.assertEqual(len(client.mutations()), 1)
        self.assertIn("尚未确认", result["errors"][0])

    def test_success_requires_final_platform_progress_not_only_resource_status(self):
        client = study_client(statuses=[response({"isFinish": False}), response({"isFinish": True})],
                              progress={"progressRate": 0.6}, write=response(None))
        result = CourseService(client, sleep=lambda _: None).study("course")
        self.assertEqual(result["completed"], 1)
        self.assertFalse(result["success"])
        self.assertFalse(result["verified"])

    def test_unknown_progress_fields_do_not_create_false_completion(self):
        result = CourseService(study_client(progress={"someUnknownValue": 100}), sleep=lambda _: None).study("course")
        self.assertFalse(result["success"])

    def test_read_failure_does_not_trigger_a_write(self):
        client = study_client(statuses=[ApiError("读取失败")])
        result = CourseService(client, sleep=lambda _: None).study("course")
        self.assertFalse(result["success"])
        self.assertEqual(result["failed"], 1)
        self.assertEqual(client.mutations(), [])

    def test_write_timeout_is_unknown_and_is_never_retried(self):
        client = study_client(statuses=[response({"isFinish": False})],
                              write=ApiError("请求超时 local-secret-token"))
        result = CourseService(client, sleep=lambda _: None).study("course")
        self.assertFalse(result["success"])
        self.assertEqual(len(client.mutations()), 1)
        self.assertIn("结果未知", result["errors"][0])
        self.assertNotIn(client.token, str(result))

    def test_cancel_before_start_performs_no_requests(self):
        client = FakeClient()
        result = CourseService(client, sleep=lambda _: None).study("course", cancel=lambda: True)
        self.assertTrue(result["cancelled"])
        self.assertFalse(result["success"])
        self.assertEqual(client.calls, [])

    def test_no_related_exam_never_uses_a_historical_default(self):
        client = FakeClient().add("coursePacket/getCourseRelateExam", response([]))
        result = CourseService(client, sleep=lambda _: None).exam("course")
        self.assertFalse(result["success"])
        self.assertIn("没有", result["message"])
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.mutations(), [])

    def test_multiple_related_exams_require_an_explicit_choice(self):
        client = FakeClient().add("coursePacket/getCourseRelateExam", response([
            {"examId": "one"}, {"examId": "two"}]))
        result = CourseService(client, sleep=lambda _: None).exam("course")
        self.assertFalse(result["success"])
        self.assertIn("多个", result["message"])
        self.assertEqual(client.mutations(), [])

    def test_missing_result_stays_pending_without_inventing_a_score(self):
        client = exam_client(after=None)
        result = CourseService(client, sleep=lambda _: None).exam("course")
        self.assertFalse(result["success"])
        self.assertTrue(result["pending"])
        self.assertTrue(result["submitted"])
        self.assertIsNone(result["score"])
        self.assertEqual(len(client.mutations()), 1)

    def test_previous_high_score_does_not_confirm_the_current_attempt(self):
        previous = {"score": 100, "answerCount": 3, "submit_date": 500}
        client = exam_client(before=previous, after=previous)
        result = CourseService(client, sleep=lambda _: None).exam("course")
        self.assertTrue(result["pending"])
        self.assertIsNone(result["score"])
        self.assertEqual(len(client.mutations()), 1)

    def test_actual_zero_score_is_preserved_in_state_update(self):
        client = exam_client(after={"score": 0, "answerCount": 1}, finish=response(None))
        result = CourseService(client, sleep=lambda _: None).exam("course")
        self.assertTrue(result["success"])
        self.assertEqual(result["score"], 0)
        self.assertEqual(client.mutations()[-1][2]["examResult"], 0)
        self.assertEqual(client.mutations()[0][2]["randomId"], "server-paper-session")

    def test_state_update_failure_is_not_reported_as_exam_success(self):
        client = exam_client(after={"score": 86}, finish={"status": 1, "message": "状态写入失败"})
        result = CourseService(client, sleep=lambda _: None).exam("course")
        self.assertFalse(result["success"])
        self.assertTrue(result["submitted"])
        self.assertEqual(result["score"], 86)
        self.assertIn("状态写入失败", result["message"])

    def test_exam_submission_timeout_is_not_retried(self):
        client = exam_client(submit=ApiError("连接已断开"))
        result = CourseService(client, sleep=lambda _: None).exam("course")
        self.assertFalse(result["success"])
        self.assertTrue(result["uncertain"])
        self.assertIn("结果未知", result["message"])
        self.assertEqual(len(client.mutations()), 1)

    def test_none_response_does_not_raise_a_secondary_attribute_error(self):
        client = exam_client()
        client.routes[("POST", API + "activity/exam/saveExamResultJc.do")].clear()
        client.routes[("POST", API + "activity/exam/saveExamResultJc.do")].append(None)
        result = CourseService(client, sleep=lambda _: None).exam("course")
        self.assertFalse(result["success"])
        self.assertIn("格式异常", result["message"])

    def test_cancel_after_submission_stops_further_requests(self):
        client = exam_client(after={"score": 100}, finish=response(None))
        cancelled = False

        def emit(message):
            nonlocal cancelled
            if message == "正在查询平台成绩":
                cancelled = True

        result = CourseService(client, sleep=lambda _: None).exam("course", emit=emit, cancel=lambda: cancelled)
        self.assertTrue(result["cancelled"])
        self.assertTrue(result["submitted"])
        self.assertEqual(len(client.mutations()), 1)

    def test_missing_answer_does_not_submit_an_empty_or_guessed_paper(self):
        client = exam_client()
        client.routes[("GET", API + "activity/exam/getExamContentData.do")].clear()
        client.add("activity/exam/getExamContentData.do", response({"randomId": "paper", "typeList": [
            {"questionList": [{"id": "question", "tqId": "paper-question"}]}]}))
        result = CourseService(client, sleep=lambda _: None).exam("course")
        self.assertFalse(result["success"])
        self.assertIn("缺少", result["message"])
        self.assertEqual(client.mutations(), [])

    def test_explicit_business_failure_does_not_continue_with_returned_exam_data(self):
        for rejected_result in (0, "0"):
            with self.subTest(result=rejected_result):
                client = FakeClient().add("coursePacket/getCourseRelateExam", {
                    "status": 0, "result": rejected_result,
                    "message": "登录状态已失效", "data": [{"examId": "must-not-be-used"}]})
                result = CourseService(client, sleep=lambda _: None).exam("course")
                self.assertFalse(result["success"])
                self.assertIn("失效", result["message"])
                self.assertEqual(len(client.calls), 1)
                self.assertEqual(client.mutations(), [])

    def test_malformed_question_lists_never_submit_a_partial_paper(self):
        question = {"id": "valid-question", "tqId": "valid-paper-question", "answer": "A"}
        valid_group = {"type": 1, "questionList": [question]}
        malformed_groups = (
            [valid_group, None],
            [{"type": 1, "questionList": [question, "invalid-question"]}],
            [{"type": 1, "questionList": {"list": [question]}}],
            [valid_group, {"type": 2, "questionList": []}],
        )
        for groups in malformed_groups:
            with self.subTest(groups=groups):
                client = exam_client()
                client.routes[("GET", API + "activity/exam/getExamContentData.do")].clear()
                client.add("activity/exam/getExamContentData.do", response({"randomId": "paper", "typeList": groups}))
                result = CourseService(client, sleep=lambda _: None).exam("course")
                self.assertFalse(result["success"])
                self.assertIn("未提交", result["message"])
                self.assertEqual(client.mutations(), [])

    def test_progress_reads_only_the_aggregate_progress_endpoint(self):
        client = FakeClient().add("coursePacket/getCourseUserStatistic", response({"progressRate": 0.75}))
        self.assertEqual(CourseService(client).progress("course"), {"progressRate": 0.75})
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.mutations(), [])

    def test_delayed_lesson_and_total_confirmation_complete_without_resubmission(self):
        client = study_client(statuses=[response({"isFinish": False}),
                                       response({"isFinish": False}), response({"isFinish": True})],
                              write=response(None))
        client.routes[("GET", API + "coursePacket/getCourseUserStatistic")].clear()
        client.add("coursePacket/getCourseUserStatistic", response({"progressRate": 0}),
                   response({"progressRate": 0.5}), response({"progressRate": 1}))
        sleeps = []
        result = CourseService(client, sleep=sleeps.append).study("course")
        self.assertTrue(result["success"])
        self.assertEqual(result["completed"], 1)
        self.assertEqual(len(client.mutations()), 1)
        self.assertEqual(sleeps, [1, 1, 1])

    def test_unconfirmed_lesson_and_progress_stop_after_three_reads_each(self):
        client = study_client(statuses=[response({"isFinish": False})] * 4,
                              progress={"progressRate": 0}, write=response(None))
        sleeps = []
        result = CourseService(client, sleep=sleeps.append).study("course")
        self.assertFalse(result["success"])
        self.assertEqual(len(client.mutations()), 1)
        status_calls = [call for call in client.calls if call[1].endswith("getResourceUserStatistic")]
        progress_calls = [call for call in client.calls if call[1].endswith("getCourseUserStatistic")]
        self.assertEqual(len(status_calls), 4)  # One preflight read, then at most three confirmations.
        self.assertEqual(len(progress_calls), 3)
        self.assertEqual(sleeps, [1] * 4)

    def test_transient_confirmation_read_failure_does_not_repeat_the_write(self):
        client = study_client(statuses=[response({"isFinish": False}), ApiError("查询暂时超时"),
                                       response({"isFinish": True})], write=response(None))
        result = CourseService(client, sleep=lambda _: None).study("course")
        self.assertTrue(result["success"])
        self.assertEqual(len(client.mutations()), 1)

    def test_cancellation_during_confirmation_delay_stops_next_request(self):
        client = study_client(statuses=[response({"isFinish": False})] * 4, write=response(None))
        cancelled = False

        def stop_on_delay(_seconds):
            nonlocal cancelled
            cancelled = True

        result = CourseService(client, sleep=stop_on_delay).study("course", cancel=lambda: cancelled)
        self.assertTrue(result["cancelled"])
        self.assertEqual(len(client.mutations()), 1)
        self.assertEqual(len([call for call in client.calls if call[1].endswith("getResourceUserStatistic")]), 2)
        self.assertFalse(any(call[1].endswith("getCourseUserStatistic") for call in client.calls))

    def test_delayed_exam_result_is_confirmed_without_submitting_again(self):
        client = exam_client(after=None, finish=response(None))
        client.routes[("GET", API + "activity/exam/getExamResultMaxScore.do")].clear()
        client.add("activity/exam/getExamResultMaxScore.do", response(None), response(None),
                   response(None), response({"score": 91, "answerCount": 1}))
        sleeps = []
        result = CourseService(client, sleep=sleeps.append).exam("course")
        self.assertTrue(result["success"])
        self.assertEqual(result["score"], 91)
        self.assertEqual(len([call for call in client.calls if call[0] == "POST"]), 1)
        self.assertEqual(len(client.mutations()), 2)
        self.assertEqual(sleeps, [1, 1])

    def test_exam_stays_pending_after_three_read_confirmations(self):
        client = exam_client(after=None)
        sleeps = []
        result = CourseService(client, sleep=sleeps.append).exam("course")
        self.assertTrue(result["pending"])
        self.assertFalse(result["success"])
        self.assertEqual(len([call for call in client.calls if call[1].endswith("getExamResultMaxScore.do")]), 4)
        self.assertEqual(len(client.mutations()), 1)
        self.assertEqual(sleeps, [1, 1])
        self.assertIn("官网", result["message"])
        self.assertIn("勿通过再次考试", result["message"])

    def test_existing_record_requires_explicit_retake_even_for_zero_score(self):
        for score in (0, 86, 100):
            with self.subTest(score=score):
                client = exam_client(before={"score": score, "answerCount": 1})
                result = CourseService(client, sleep=lambda _: None).exam("course", allow_retake=False)
                self.assertFalse(result["success"])
                self.assertTrue(result["existing"])
                self.assertEqual(result["score"], score)
                self.assertFalse(result["submitted"])
                self.assertEqual(len(client.calls), 2)
                self.assertFalse(any(call[1].endswith("getExamContentData.do") for call in client.calls))
                self.assertEqual(client.mutations(), [])


if __name__ == "__main__":
    unittest.main()
