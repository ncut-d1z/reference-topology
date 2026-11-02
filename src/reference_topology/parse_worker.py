"""Isolated PDF parser command line used by the durable job runner.

Exit 0: a result (succeeded/partial/needs_ocr) was written.
Exit 1: a structured failed result was written, when the output was writable.
The parent remains responsible for hard process timeouts on every platform.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import signal
import sys
import uuid
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reference_topology.parsing import PARSER_VERSION, ParseError, parse_pdf


def _limits(memory_mb: int, cpu_seconds: int) -> list[str]:
    warnings: list[str] = []
    try:
        import resource
    except ImportError:
        return ["worker_resource_limits_unavailable: parent process must enforce the wall-clock timeout."]
    for kind, value, name in (
        (resource.RLIMIT_AS, memory_mb * 1024 * 1024, "address-space"),
        (resource.RLIMIT_CPU, cpu_seconds, "cpu-time"),
    ):
        if value <= 0:
            continue
        try:
            soft, hard = resource.getrlimit(kind)
            new_soft = min(value, soft) if soft != resource.RLIM_INFINITY else value
            if hard != resource.RLIM_INFINITY:
                new_soft = min(new_soft, hard)
            # Do not loosen an inherited sandbox limit or alter the hard limit.
            resource.setrlimit(kind, (new_soft, hard))
        except (ValueError, OSError):
            warnings.append(f"worker_resource_limit_unavailable: {name}.")
    return warnings


def _write_atomic(path: Path, result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Parse a PDF in an isolated worker.")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--engine", default="python-rules", choices=("python-rules", "grobid-local"))
    parser.add_argument("--grobid-url", default="http://127.0.0.1:8070")
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--max-pages", type=int, default=1000)
    parser.add_argument("--memory-mb", type=int, default=1024)
    parser.add_argument("--cpu-seconds", type=int)
    parser.add_argument("--no-resource-limits", action="store_true")
    args = parser.parse_args(argv)
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    logging.getLogger("pdfminer").setLevel(logging.ERROR)
    warnings: list[str] = []
    result: dict[str, Any]
    exit_code = 0

    def timed_out(signum: int, frame: Any) -> None:
        raise ParseError("parse_timeout", "PDF worker exceeded the configured time limit.")

    alarm_enabled = False
    try:
        if not math.isfinite(args.timeout) or args.timeout <= 0 or args.max_pages < 1:
            raise ParseError("invalid_limits", "Timeout and page limit must be positive and finite.")
        if args.input.resolve() == args.output.resolve():
            raise ParseError("output_conflicts_with_input", "The worker output must not overwrite the source PDF.")
        if not args.no_resource_limits:
            warnings.extend(_limits(args.memory_mb, args.cpu_seconds or math.ceil(args.timeout) + 1))
        if hasattr(signal, "SIGALRM") and hasattr(signal, "setitimer"):
            signal.signal(signal.SIGALRM, timed_out)
            signal.setitimer(signal.ITIMER_REAL, args.timeout)
            alarm_enabled = True
        result = parse_pdf(args.input, engine=args.engine, grobid_url=args.grobid_url,
                           timeout=args.timeout, max_pages=args.max_pages)
        result.setdefault("warnings", []).extend(warnings)
    except ParseError as exc:
        exit_code = 1
        result = {"status": "failed", "error": {"code": exc.code, "message": str(exc)},
                  "engine": args.engine, "engine_version": PARSER_VERSION,
                  "metadata": {}, "references": [], "warnings": warnings, "page_count": 0}
    except (Exception, MemoryError) as exc:
        exit_code = 1
        code = "worker_memory_limit" if isinstance(exc, MemoryError) else "worker_failed"
        result = {"status": "failed", "error": {"code": code, "message": f"PDF worker failed ({type(exc).__name__})."},
                  "engine": args.engine, "engine_version": PARSER_VERSION,
                  "metadata": {}, "references": [], "warnings": warnings, "page_count": 0}
    finally:
        if alarm_enabled:
            signal.setitimer(signal.ITIMER_REAL, 0)
    try:
        # Never write a failed-result envelope over the PDF even in the error path.
        if args.input.resolve() == args.output.resolve():
            raise OSError("Source and output paths must differ.")
        _write_atomic(args.output, result)
    except OSError:
        print(json.dumps({"status": "failed", "error": {"code": "worker_output_unwritable",
              "message": "The worker could not write its result file."}}), file=sys.stderr)
        return 1
    if exit_code:
        print(json.dumps(result["error"], ensure_ascii=False), file=sys.stderr)
    print(json.dumps({"status": result["status"], "output": str(args.output)}, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
