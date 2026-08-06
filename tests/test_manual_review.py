import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ManualReviewTests(unittest.TestCase):
    def test_failed_standard_and_recording_tasks_enter_review_queue(self):
        app_source = (ROOT / "potatoflow-app" / "app.py").read_text(encoding="utf-8")

        self.assertIn("failed_tasks = get_tasks_by_status(TASK_STATES['FAILED'])", app_source)
        self.assertIn("statuses={'failed'}", app_source)
        self.assertIn("recording_jobs=recording_review_jobs", app_source)

    def test_review_page_exposes_recording_failure_details_and_retry(self):
        template = (
            ROOT / "potatoflow-app" / "templates" / "manual_review.html"
        ).read_text(encoding="utf-8")

        self.assertIn("录播失败任务", template)
        self.assertNotIn("查看流水线与日志", template)
        self.assertIn("进入编辑审核", template)
        self.assertIn("live_recording_job_review", template)
        self.assertIn("live_recording_job_delete", template)

    def test_recording_review_has_full_editor_and_persistent_override(self):
        app_source = (ROOT / "potatoflow-app" / "app.py").read_text(encoding="utf-8")
        manager_source = (
            ROOT / "potatoflow-app" / "modules" / "live_recorder_manager.py"
        ).read_text(encoding="utf-8")
        bridge_source = (ROOT / "potatoflow-app" / "bridge.py").read_text(encoding="utf-8")
        template = (
            ROOT / "potatoflow-app" / "templates" / "recording_review_edit.html"
        ).read_text(encoding="utf-8")

        self.assertIn("def live_recording_job_review(fingerprint)", app_source)
        self.assertIn("save_pipeline_review(", app_source)
        self.assertIn("tags=tags if tags_submitted else job.get('tags', [])", app_source)
        self.assertIn("if 'description' in request.form", app_source)
        self.assertIn("确认并继续生成封面与投稿", template)
        self.assertIn('name="cover_file"', template)
        self.assertIn('name="partition_id"', template)
        self.assertIn("recording_review_overrides", manager_source)
        self.assertIn("review_override = store.review_override(key)", bridge_source)
        self.assertIn('"manual_review_applied": True', bridge_source)

    def test_overview_review_count_includes_failed_recordings(self):
        app_source = (ROOT / "potatoflow-app" / "app.py").read_text(encoding="utf-8")

        self.assertIn(
            "awaiting_review += sum(job.get('status') == 'failed' for job in recording_jobs)",
            app_source,
        )

    def test_completed_recording_can_regenerate_and_confirm_published_metadata(self):
        app_source = (ROOT / "potatoflow-app" / "app.py").read_text(encoding="utf-8")
        manager_source = (
            ROOT / "potatoflow-app" / "modules" / "live_recorder_manager.py"
        ).read_text(encoding="utf-8")
        tasks_template = (
            ROOT / "potatoflow-app" / "templates" / "tasks.html"
        ).read_text(encoding="utf-8")
        editor_template = (
            ROOT / "potatoflow-app" / "templates" / "recording_review_edit.html"
        ).read_text(encoding="utf-8")

        self.assertIn("AI 编辑稿件", tasks_template)
        for action in (
            "regenerate_title",
            "regenerate_description",
            "regenerate_tags",
            "regenerate_cover_16x9",
            "regenerate_cover_4x3",
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

    def test_running_recording_can_be_interrupted_after_ai_before_cover(self):
        app_source = (ROOT / "potatoflow-app" / "app.py").read_text(encoding="utf-8")
        manager_source = (
            ROOT / "potatoflow-app" / "modules" / "live_recorder_manager.py"
        ).read_text(encoding="utf-8")
        bridge_source = (ROOT / "potatoflow-app" / "bridge.py").read_text(encoding="utf-8")
        tasks_template = (
            ROOT / "potatoflow-app" / "templates" / "tasks.html"
        ).read_text(encoding="utf-8")
        editor_template = (
            ROOT / "potatoflow-app" / "templates" / "recording_review_edit.html"
        ).read_text(encoding="utf-8")

        self.assertIn("live_recording_job_review_hold", app_source)
        self.assertIn("request_pipeline_ai_review", manager_source)
        self.assertIn('latest_review_override.get("hold_before_cover")', bridge_source)
        hold_index = bridge_source.index('latest_review_override.get("hold_before_cover")')
        self.assertLess(
            hold_index,
            bridge_source.index('current_stage = "cover_16x9"', hold_index),
        )
        self.assertIn("AI 完成后可介入", tasks_template)
        self.assertIn("立即介入并编辑", tasks_template)
        self.assertIn("无需提前设置", tasks_template)
        self.assertIn("PRE-PUBLISH AI REVIEW", editor_template)
        self.assertIn("先生成简介，再基于新简介生成标题", editor_template)
        self.assertIn('value="save_and_continue"', editor_template)

    def test_ai_pipeline_generates_verified_description_before_title(self):
        bridge_source = (ROOT / "potatoflow-app" / "bridge.py").read_text(
            encoding="utf-8"
        )

        description_call = '"recording_danmaku_summary_batch"'
        title_call = 'scene_name="recording_danmaku_title_from_description"'
        self.assertLess(
            bridge_source.index(description_call),
            bridge_source.index(title_call),
        )
        self.assertIn('"final_description": final_description', bridge_source)

    def test_ai_regenerate_action_is_serialized_before_button_is_disabled(self):
        editor_template = (
            ROOT / "potatoflow-app" / "templates" / "recording_review_edit.html"
        ).read_text(encoding="utf-8")

        self.assertIn("which would drop action=regenerate_*", editor_template)
        self.assertIn("window.setTimeout(function ()", editor_template)
        self.assertLess(
            editor_template.index("window.setTimeout(function ()"),
            editor_template.index("submitter.disabled = true"),
        )

    def test_each_live_room_can_override_ai_prompts_with_visible_defaults(self):
        app_source = (ROOT / "potatoflow-app" / "app.py").read_text(encoding="utf-8")
        manager_source = (
            ROOT / "potatoflow-app" / "modules" / "live_recorder_manager.py"
        ).read_text(encoding="utf-8")
        live_template = (
            ROOT / "potatoflow-app" / "templates" / "live_recording.html"
        ).read_text(encoding="utf-8")

        self.assertIn("live_recording_room_prompts", app_source)
        self.assertIn("def save_room_prompts(", manager_source)
        self.assertIn('"ai_title_prompt"', manager_source)
        self.assertIn('"ai_description_prompt"', manager_source)
        self.assertIn('"ai_cover_prompt"', manager_source)
        self.assertIn("查看当前继承的三个提示词", live_template)
        self.assertIn("留空继承全局设置", live_template)
        self.assertIn('name="ai_title_prompt"', live_template)
        self.assertIn('name="ai_description_prompt"', live_template)
        self.assertIn('name="ai_cover_prompt"', live_template)
        self.assertIn('name="ai_danmaku_reaction_delay_seconds"', live_template)
        self.assertIn("默认提前 8 秒", live_template)
        self.assertIn('name="cover_reference_file"', live_template)
        self.assertIn("封面人物底稿", live_template)
        self.assertIn("保存 AI 设置", live_template)

        review_template = (
            ROOT / "potatoflow-app" / "templates" / "recording_review_edit.html"
        ).read_text(encoding="utf-8")
        self.assertIn("重新生成时间点验证", review_template)
        self.assertIn("timeline.timeline_verified_count", review_template)

    def test_recording_detail_explains_every_pipeline_status_in_text(self):
        tasks_template = (
            ROOT / "potatoflow-app" / "templates" / "tasks.html"
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
        self.assertIn("等待 AI 简介/标题队列", tasks_template)
        self.assertIn("ai_metadata_queue_wait_seconds", tasks_template)


if __name__ == "__main__":
    unittest.main()
