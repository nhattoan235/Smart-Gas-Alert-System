from __future__ import annotations

import unittest
from pathlib import Path

from verify_nvs_runtime_vcd import VcdAssertionError, inspect_vcd_text, validate_runtime_vcd_path


def make_vcd(red_values: str, buzzer_values: str) -> str:
    events = ["$scope module logic $end", "$var wire 1 ! BUZZER $end", "$var wire 1 $ RED $end",
              "$upscope $end", "$enddefinitions $end", "#0", "0!", "0$"]
    timestamp = 10
    for value in red_values:
        events.extend((f"#{timestamp}", f"{value}$"))
        timestamp += 10
    for value in buzzer_values:
        events.extend((f"#{timestamp}", f"{value}!"))
        timestamp += 10
    return "\n".join(events) + "\n"


class NvsRuntimeVcdTests(unittest.TestCase):
    def test_red_high_and_buzzer_edges_pass(self) -> None:
        evidence = inspect_vcd_text(make_vcd("1", "01010101010101010101"))
        self.assertTrue(evidence["red_high"])
        self.assertGreaterEqual(evidence["buzzer_transitions"], 10)

    def test_no_red_alarm_level_fails(self) -> None:
        with self.assertRaisesRegex(VcdAssertionError, "never observed GPIO7/RED high"):
            inspect_vcd_text(make_vcd("0", "0101010101010101"))

    def test_flat_buzzer_fails_even_if_red_is_high(self) -> None:
        with self.assertRaisesRegex(VcdAssertionError, "buzzer pin levels"):
            inspect_vcd_text(make_vcd("1", "0000000000000000"))

    def test_too_few_buzzer_transitions_fail(self) -> None:
        with self.assertRaisesRegex(VcdAssertionError, "only 2 buzzer transitions"):
            inspect_vcd_text(make_vcd("1", "010"))

    def test_missing_vcd_channel_fails(self) -> None:
        with self.assertRaisesRegex(VcdAssertionError, "missing required analyzer channels"):
            inspect_vcd_text("$scope module logic $end\n$var wire 1 ! RED $end\n$upscope $end")

    def test_older_or_unrelated_vcd_path_is_rejected(self) -> None:
        with self.assertRaisesRegex(VcdAssertionError, "unrelated/older VCD"):
            validate_runtime_vcd_path(Path("test-results/old/node1-gpio.vcd"))

    def test_current_run_vcd_path_is_accepted(self) -> None:
        validate_runtime_vcd_path(Path(
            "test-results/20261116T054154922Z/"
            "nvs-runtime-wokwi-20261116T054154922Z/nvs-runtime-gpio.vcd"
        ))

    def test_vcd_from_different_run_folder_is_rejected(self) -> None:
        with self.assertRaisesRegex(VcdAssertionError, "unrelated/older VCD"):
            validate_runtime_vcd_path(Path(
                "test-results/20261116T054154922Z/"
                "nvs-runtime-wokwi-20261116T052932627Z/nvs-runtime-gpio.vcd"
            ))


if __name__ == "__main__":
    unittest.main()
