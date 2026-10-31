"""Durable single-worker queue; PDF parsing runs in a terminable subprocess."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid


class JobRunner:
    def __init__(self, settings, store, files):
        self.settings, self.store, self.files = settings, store, files
        self._stop = threading.Event()
        self._thread = None

    def enqueue(self, file_id, paper_id, *, engine=None, force=False, request_key=None):
        engine = engine or self.settings.engine
        if engine not in {"python-rules", "grobid-local"}:
            raise ValueError("未知解析器")
        configuration = {"engine": engine, "grobid_url": self.settings.grobid_url,
                         "max_pages": self.settings.max_pdf_pages, "timeout": self.settings.timeout_seconds,
                         "consolidate_header": False, "consolidate_citations": False, "consolidate_funders": False}
        config_hash = hashlib.sha256(json.dumps(configuration, sort_keys=True).encode()).hexdigest()
        work_key = f"parse:{file_id}:{config_hash}"
        if not force:
            for job in self.store.list_jobs(limit=1000):
                if job["work_key"] == work_key and job["status"] in {"queued", "running", "succeeded", "partial"}:
                    return job
        return self.store.create_job("parse", asset_id=file_id, paper_id=paper_id,
                    request_key=request_key or uuid.uuid4().hex, work_key=work_key,
                    input_data={**configuration, "config_hash": config_hash})

    def start(self):
        if self._thread and self._thread.is_alive():
            if self._stop.is_set():
                raise RuntimeError("后台解析队列仍在停止，请稍后重试")
            return
        self._stop.clear()
        self.store.recover_jobs()
        self._thread = threading.Thread(target=self._loop, name="reference-parser", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=6)

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.store.recover_jobs()
                job = self.store.claim_job(lease_seconds=30)
                if job:
                    self.run(job)
            except Exception:
                # A transient database failure must not permanently kill the queue.
                self._stop.wait(1)
            self._stop.wait(0.25)

    def run(self, job):
        snapshot = None
        process = None
        output_path = None
        token = job["lease_token"]
        try:
            configuration = job.get("input_data") or job.get("input_json") or {}
            if isinstance(configuration, str):
                configuration = json.loads(configuration)
            snapshot, _ = self.files.snapshot(job["asset_id"])
            artifact_dir = self.settings.data_dir / "artifacts" / job["id"]
            artifact_dir.mkdir(parents=True, exist_ok=True)
            output_path = artifact_dir / f"attempt-{job['attempt']}-{uuid.uuid4().hex}.json"
            command = [sys.executable, "-m", "reference_topology.parse_worker",
                       "--input", str(snapshot), "--output", str(output_path),
                       "--engine", configuration["engine"], "--grobid-url", configuration["grobid_url"],
                       "--timeout", str(configuration["timeout"]), "--max-pages", str(configuration["max_pages"])]
            environment = dict(os.environ)
            # Source checkout and installed wheel both work; no inherited shell command evaluation.
            package_root = str(Path(__file__).resolve().parent.parent)
            environment["PYTHONPATH"] = package_root + os.pathsep + environment.get("PYTHONPATH", "")
            process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=environment)
            deadline = time.monotonic() + configuration["timeout"] + 5
            last_heartbeat = 0
            while process.poll() is None:
                current = self.store.get_job(job["id"])
                if current["status"] == "cancelled" or self._stop.is_set():
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2)
                    if current["status"] != "cancelled":
                        self.store.update_job(job["id"], lease_token=token, status="interrupted",
                                              error_code="shutdown", error_message="服务停止，任务可重试")
                    return
                now = time.monotonic()
                if now > deadline:
                    process.kill()
                    process.wait(timeout=2)
                    raise TimeoutError("解析超过时间上限")
                if now - last_heartbeat > 2:
                    self.store.update_job(job["id"], lease_token=token, lease_seconds=30,
                                          progress_current=1, progress_total=3)
                    last_heartbeat = now
                self._stop.wait(0.15)
            if not output_path.exists():
                raise RuntimeError("解析进程未返回结果，可能超过资源限制")
            result = json.loads(output_path.read_text(encoding="utf-8"))
            if result.get("status") == "failed":
                error = result.get("error", {})
                self.store.update_job(job["id"], lease_token=token, status="failed",
                    error_code=error.get("code", "parse_failed"), error_message=error.get("message", "解析失败"))
                return
            if process.returncode:
                raise RuntimeError("解析进程异常退出")
            # Publishing also finishes the job in the same SQLite transaction.
            # A cancellation can win before that transaction, never after a
            # visible snapshot while the job still appears to be running.
            self.store.publish_parse(job["asset_id"], job["paper_id"], result,
                    config_hash=configuration["config_hash"], output_uri=output_path.as_uri(),
                    job_id=job["id"], lease_token=token)
        except Exception as error:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=2)
            try:
                current = self.store.get_job(job["id"])
                if current["status"] == "running":
                    self.store.update_job(job["id"], lease_token=token, status="failed",
                        error_code=getattr(error, "code", "parse_timeout" if isinstance(error, TimeoutError) else "parse_failed"),
                        error_message=str(error)[:500])
            except Exception:
                pass  # Lost lease: never overwrite the new worker's result.
        finally:
            if snapshot:
                snapshot.unlink(missing_ok=True)
