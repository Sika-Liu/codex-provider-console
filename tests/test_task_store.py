import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from application.task_store import TaskStore

try:
    import fastapi  # noqa: F401
except ImportError:
    FASTAPI_UNAVAILABLE = True
else:
    FASTAPI_UNAVAILABLE = False


ROOT = Path(__file__).resolve().parents[1]


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

    def test_prune_keeps_running_jobs_and_bounds_finished_history(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory) / "jobs.sqlite3")
            store.new_job("health", {"id": "running", "status": "running", "stage": "运行中"})
            for index in range(3):
                store.new_job("health", {
                    "id": f"finished-{index}", "status": "completed", "stage": "完成",
                })
            old = time.time() - 9 * 24 * 60 * 60
            with store._connect() as connection:
                for index in range(3):
                    connection.execute(
                        "UPDATE background_jobs SET updated_at=? WHERE id=?",
                        (old + index, f"finished-{index}"),
                    )

            removed = store.prune_finished(now=time.time(), retention_seconds=24 * 60 * 60, max_finished=1)
            ids = {job["id"] for _, job in store.recover()}

            self.assertEqual(removed, 3)
            self.assertEqual(ids, {"running"})

    @unittest.skipIf(FASTAPI_UNAVAILABLE, "FastAPI runtime is unavailable on the host")
    def test_application_startup_marks_persisted_running_task_interrupted(self):
        with tempfile.TemporaryDirectory() as directory:
            codex_home = Path(directory)
            database = codex_home / "control-panel-jobs.sqlite3"
            TaskStore(database).new_job(
                "health", {"id": "startup-job", "status": "running", "stage": "运行中"}
            )
            environment = os.environ | {"CODEX_HOME": str(codex_home)}
            completed = subprocess.run(
                [sys.executable, "-c", "import app"],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

            _, job = TaskStore(database).recover()[0]
            self.assertEqual(job["status"], "interrupted")


if __name__ == "__main__":
    unittest.main()
