import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = ROOT / "potatoflow-app"


class CoverRollbackTests(unittest.TestCase):
    def test_removed_cover_modes_do_not_reappear(self):
        checked = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (
                APP_ROOT / "bridge.py",
                APP_ROOT / "app.py",
                APP_ROOT / "modules" / "config_manager.py",
                APP_ROOT / "templates" / "settings.html",
            )
        )
        for removed in (
            "RECORDING_DOTA2_COVER_LAYOUT_MODE",
            "DOTA2_COVER_LAYOUT_FUSION",
            "英雄融合构图",
            "外围错落环绕",
            "人物出镜白名单",
            "封面主角身份锁定",
            "表情与本段内容联动",
        ):
            self.assertNotIn(removed, checked)

    def test_compact_default_cover_prompt_is_active(self):
        spec = importlib.util.spec_from_file_location(
            "bridge_cover_rollback",
            APP_ROOT / "bridge.py",
        )
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        prompt = module.DEFAULT_RECORDING_COVER_AI_PROMPT
        self.assertIn("画面精致、主体明确、对比强烈", prompt)
        self.assertIn("必须逐字保留，不得改写、重复、漏字", prompt)
        self.assertIn("文字区最多占画面约四成", prompt)
        self.assertNotIn("Cos", prompt)
        self.assertNotIn("装备", prompt)


if __name__ == "__main__":
    unittest.main()
