import io
import unittest
from pathlib import Path

from evaluate_trace import EvaluationInputError, _latency_stats, evaluate, read_trace


FIXTURE = Path(__file__).parent / "fixtures" / "metric-smoke.csv"


class TraceEvaluationTests(unittest.TestCase):
    def test_smoke_fixture_calculates_known_counts_and_marker_latency(self):
        with FIXTURE.open("r", encoding="utf-8", newline="") as stream:
            rows, columns = read_trace(stream)
        result = evaluate(rows, columns)

        self.assertEqual(result["source_type"], "synthetic")
        classification = result["classification"]
        self.assertEqual(classification["status"], "MEASURED")
        self.assertEqual(classification["confusion"], {"tp": 1, "fp": 1, "fn": 4, "tn": 2})
        self.assertEqual(classification["evaluated_samples"], 8)
        self.assertEqual(classification["excluded_non_evaluable_rows"], {"fault": 1, "invalid": 1})
        self.assertEqual(classification["precision"], 0.5)
        self.assertEqual(classification["recall"], 0.2)
        self.assertEqual(classification["false_alarm_rate"], round(1 / 3, 6))
        self.assertEqual(classification["miss_rate"], 0.8)
        self.assertEqual(classification["untimestamped_rows_excluded"], 1)
        self.assertEqual(result["availability_exclusions"]["counts"]["fault_rows"], 1)
        self.assertEqual(result["availability_exclusions"]["counts"]["invalid_rows"], 2)
        latency = result["episode_latency"]
        self.assertEqual(latency["status"], "PARTIAL")
        self.assertEqual(latency["measured_delays_ms"], [200])
        self.assertEqual(latency["p50_delay_ms"], 200)
        self.assertIsNone(latency["p95_delay_ms"])
        self.assertEqual(latency["episode_marker_counts"], {"measured": 1, "missing_first_alarm": 1})

    def test_p50_and_nearest_rank_p95(self):
        small = _latency_stats([10, 20, 30, 40])
        self.assertEqual(small["p50_delay_ms"], 25)
        self.assertIsNone(small["p95_delay_ms"])
        self.assertIn("at least 20", small["p95_na_reason"])
        enough = _latency_stats(list(range(20)))
        self.assertEqual(enough["p50_delay_ms"], 9.5)
        self.assertEqual(enough["p95_delay_ms"], 18)

    def test_missing_timestamps_produce_na_not_guessed_metrics(self):
        data = "source_type,run_id,scenario,device_id,gas,truth_hazard,truth_episode_id,state,valid\n"
        data += "synthetic,r1,s1,node1,LPG,true,e1,DANGER,true\n"
        rows, columns = read_trace(io.StringIO(data))
        result = evaluate(rows, columns)
        self.assertEqual(result["classification"]["status"], "NA")
        self.assertIsNone(result["classification"]["precision"])
        self.assertEqual(result["episode_latency"]["status"], "NA")
        self.assertEqual(result["episode_latency"]["na_reason"], "hazard_start_ms and first_alarm_ms columns are required")

    def test_source_type_is_validated_and_mixed_files_rejected(self):
        header = "source_type,run_id,scenario,device_id,gas,truth_hazard,state,valid\n"
        with self.assertRaises(EvaluationInputError):
            read_trace(io.StringIO(header + "made-up,r1,s1,node1,LPG,false,SAFE,true\n"))
        mixed = header + "synthetic,r1,s1,node1,LPG,false,SAFE,true\n"
        mixed += "wokwi,r1,s1,node1,LPG,false,SAFE,true\n"
        with self.assertRaisesRegex(EvaluationInputError, "exactly one source_type"):
            read_trace(io.StringIO(mixed))

    def test_non_monotonic_sample_timestamps_rejected_per_stream(self):
        data = "source_type,run_id,scenario,device_id,gas,sample_time_ms,truth_hazard,state,valid\n"
        data += "synthetic,r1,s1,node1,CO,20,false,SAFE,true\n"
        data += "synthetic,r1,s1,node1,CO,20,true,DANGER,true\n"
        with self.assertRaisesRegex(EvaluationInputError, "sample_time_ms must increase"):
            read_trace(io.StringIO(data))

    def test_prealert_is_not_returned_as_negative_latency(self):
        data = "source_type,run_id,scenario,device_id,gas,sample_time_ms,truth_hazard,truth_episode_id,hazard_start_ms,first_alarm_ms,state,valid\n"
        data += "integration,r1,s1,node2,CO,100,true,e1,200,150,WARNING,true\n"
        rows, columns = read_trace(io.StringIO(data))
        result = evaluate(rows, columns)
        latency = result["episode_latency"]
        self.assertEqual(latency["status"], "NA")
        self.assertEqual(latency["episode_marker_counts"], {"prealert": 1})
        self.assertEqual(latency["measured_delays_ms"], [])


if __name__ == "__main__":
    unittest.main()
