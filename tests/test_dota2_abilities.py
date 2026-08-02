import tempfile
import unittest
import sys
from pathlib import Path
from unittest.mock import patch

from PIL import Image

APP_ROOT = Path(__file__).resolve().parents[1] / "potatoflow-app"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from dota2_abilities import (
    build_dota2_ability_reference_sheet,
    dota2_ability_prompt_instruction,
    match_dota2_abilities,
)


class Dota2AbilityTests(unittest.TestCase):
    def test_matches_stable_community_aliases(self):
        matches = match_dota2_abilities("蓝猫飞脸后被斧王跳吼留下")

        self.assertEqual(
            [match.ability.english_name for match in matches],
            ["Ball Lightning", "Berserker's Call"],
        )
        instruction = dota2_ability_prompt_instruction(matches)
        self.assertIn("风暴之灵", instruction)
        self.assertIn("狂战士之吼", instruction)

    def test_ignores_ambiguous_single_action_words(self):
        self.assertEqual(match_dota2_abilities("他往前滚了一下再拉回来"), [])

    def test_builds_reference_sheet_from_official_icons(self):
        matches = match_dota2_abilities("卡尔天火命中")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            icon = root / "icon.png"
            Image.new("RGBA", (128, 128), "#3366ff").save(icon)
            output = root / "abilities.png"
            with patch(
                "dota2_abilities.download_dota2_ability_icon",
                return_value=icon,
            ):
                result, errors = build_dota2_ability_reference_sheet(
                    matches,
                    root / "cache",
                    output,
                )

            self.assertEqual(errors, [])
            self.assertEqual(result, output)
            self.assertTrue(output.is_file())


if __name__ == "__main__":
    unittest.main()
