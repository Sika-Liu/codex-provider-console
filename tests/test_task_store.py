import tempfile
import unittest
from pathlib import Path

from application.task_store import TaskStore


class TaskStoreTests(unittest.TestCase):
    def test_job_state_survives_a_new_store_instance(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "jobs.sqlite3"
            first = TaskStore(database)
            job = first.new_job("health", {"id": "job-1", "status": "completed", "stage": "完成"})
            job.update(detail="检查通过")

            recovered = TaskStore(database).recover()

            self.assertEqual(recovered, [("health", {
                "id": "job-1", "status": "completed", "stage": "完成",
                "detail": "检查通过", "updated_at": recovered[0][1]["updated_at"],
            })])

    def test_running_job_is_marked_interrupted_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "jobs.sqlite3"
            TaskStore(database).new_job(
                "model_diagnostic",
                {"id": "job-2", "status": "running", "stage": "请求上游"},
            )

            kind, job = TaskStore(database).recover()[0]

            self.assertEqual(kind, "model_diagnostic")
            self.assertEqual(job["status"], "interrupted")
            self.assertEqual(job["stage"], "服务重启，任务已中断")
            self.assertIn("重新执行", job["detail"])


if __name__ == "__main__":
    unittest.main()
