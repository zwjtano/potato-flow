import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ManualReviewTests(unittest.TestCase):
    def test_failed_standard_and_recording_tasks_enter_review_queue(self):
        app_source = (ROOT / "y2a-auto" / "app.py").read_text(encoding="utf-8")

        self.assertIn("failed_tasks = get_tasks_by_status(TASK_STATES['FAILED'])", app_source)
        self.assertIn("if job.get('status') == 'failed'", app_source)
        self.assertIn("recording_jobs=recording_review_jobs", app_source)

    def test_review_page_exposes_recording_failure_details_and_retry(self):
        template = (
            ROOT / "y2a-auto" / "templates" / "manual_review.html"
        ).read_text(encoding="utf-8")

        self.assertIn("录播失败任务", template)
        self.assertNotIn("查看流水线与日志", template)
        self.assertIn("进入编辑审核", template)
        self.assertIn("live_recording_job_review", template)
        self.assertIn("live_recording_job_delete", template)

    def test_recording_review_has_full_editor_and_persistent_override(self):
        app_source = (ROOT / "y2a-auto" / "app.py").read_text(encoding="utf-8")
        manager_source = (
            ROOT / "y2a-auto" / "modules" / "live_recorder_manager.py"
        ).read_text(encoding="utf-8")
        bridge_source = (ROOT / "bridge.py").read_text(encoding="utf-8")
        template = (
            ROOT / "y2a-auto" / "templates" / "recording_review_edit.html"
        ).read_text(encoding="utf-8")

        self.assertIn("def live_recording_job_review(fingerprint)", app_source)
        self.assertIn("save_pipeline_review(", app_source)
        self.assertIn("保存并重新投稿", template)
        self.assertIn('name="cover_file"', template)
        self.assertIn('name="partition_id"', template)
        self.assertIn("recording_review_overrides", manager_source)
        self.assertIn("review_override = store.review_override(key)", bridge_source)
        self.assertIn('"manual_review_applied": True', bridge_source)

    def test_overview_review_count_includes_failed_recordings(self):
        app_source = (ROOT / "y2a-auto" / "app.py").read_text(encoding="utf-8")

        self.assertIn(
            "awaiting_review += sum(job.get('status') == 'failed' for job in recording_jobs)",
            app_source,
        )

    def test_completed_recording_can_regenerate_and_confirm_published_metadata(self):
        app_source = (ROOT / "y2a-auto" / "app.py").read_text(encoding="utf-8")
        manager_source = (
            ROOT / "y2a-auto" / "modules" / "live_recorder_manager.py"
        ).read_text(encoding="utf-8")
        tasks_template = (
            ROOT / "y2a-auto" / "templates" / "tasks.html"
        ).read_text(encoding="utf-8")
        editor_template = (
            ROOT / "y2a-auto" / "templates" / "recording_review_edit.html"
        ).read_text(encoding="utf-8")

        self.assertIn("AI 编辑稿件", tasks_template)
        for action in (
            "regenerate_title",
            "regenerate_description",
            "regenerate_tags",
            "regenerate_cover",
            "regenerate_all",
            "apply_to_bilibili",
            "apply_to_bilibili_and_comment",
        ):
            self.assertIn(action, editor_template)
            self.assertIn(action, app_source)
        self.assertIn("def regenerate_published_metadata(", manager_source)
        self.assertIn("def update_published_metadata(", manager_source)
        self.assertIn("resolve_account, resolve_cookie_path", manager_source)
        self.assertIn("recording_dir=video_path.parent", manager_source)
        self.assertIn("pending_published_update", manager_source)
        self.assertIn("视频内容和原有分P不会改变", editor_template)
        self.assertIn("publishedUpdateConfirmModal", editor_template)
        self.assertIn("form.requestSubmit(pendingPublishedUpdateButton", editor_template)
        self.assertIn("同步稿件并更新置顶评论", editor_template)
        self.assertIn("sync_published_description_comment", manager_source)
        self.assertNotIn("window.confirm(", editor_template)
        self.assertIn('html[data-theme="dark"] .ai-regenerate-panel', editor_template)

    def test_ai_regenerate_action_is_serialized_before_button_is_disabled(self):
        editor_template = (
            ROOT / "y2a-auto" / "templates" / "recording_review_edit.html"
        ).read_text(encoding="utf-8")

        self.assertIn("which would drop action=regenerate_*", editor_template)
        self.assertIn("window.setTimeout(function ()", editor_template)
        self.assertLess(
            editor_template.index("window.setTimeout(function ()"),
            editor_template.index("submitter.disabled = true"),
        )

    def test_each_live_room_can_override_ai_prompts_with_visible_defaults(self):
        app_source = (ROOT / "y2a-auto" / "app.py").read_text(encoding="utf-8")
        manager_source = (
            ROOT / "y2a-auto" / "modules" / "live_recorder_manager.py"
        ).read_text(encoding="utf-8")
        live_template = (
            ROOT / "y2a-auto" / "templates" / "live_recording.html"
        ).read_text(encoding="utf-8")

        self.assertIn("live_recording_room_prompts", app_source)
        self.assertIn("def save_room_prompts(", manager_source)
        self.assertIn('"ai_title_prompt"', manager_source)
        self.assertIn('"ai_description_prompt"', manager_source)
        self.assertIn('"ai_cover_prompt"', manager_source)
        self.assertIn("查看三个系统默认提示词", live_template)
        self.assertIn('name="ai_title_prompt"', live_template)
        self.assertIn('name="ai_description_prompt"', live_template)
        self.assertIn('name="ai_cover_prompt"', live_template)
        self.assertIn('name="ai_danmaku_reaction_delay_seconds"', live_template)
        self.assertIn("默认提前 8 秒", live_template)
        self.assertIn('name="cover_reference_file"', live_template)
        self.assertIn("封面人物底稿", live_template)
        self.assertIn("保存 AI 设置", live_template)

        review_template = (
            ROOT / "y2a-auto" / "templates" / "recording_review_edit.html"
        ).read_text(encoding="utf-8")
        self.assertIn("重新生成时间点验证", review_template)
        self.assertIn("timeline.timeline_verified_count", review_template)

    def test_recording_detail_explains_every_pipeline_status_in_text(self):
        tasks_template = (
            ROOT / "y2a-auto" / "templates" / "tasks.html"
        ).read_text(encoding="utf-8")
        self.assertIn("XML 验证通过时间点", tasks_template)
        self.assertIn("时间点提前补偿（秒）", tasks_template)

        self.assertIn("状态说明", tasks_template)
        for label in ("绿色：已完成", "蓝色：处理中", "黄色：等待中", "灰色：已跳过", "红色：失败"):
            self.assertIn(label, tasks_template)
        self.assertIn(
            "recording-detail-stage-status ${escapeRecordingDetail(status)}",
            tasks_template,
        )


if __name__ == "__main__":
    unittest.main()
