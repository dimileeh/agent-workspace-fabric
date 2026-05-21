from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from typing import TextIO

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
Sleeper = Callable[[float], None]


def run_with_retries(
    command: Sequence[str],
    *,
    attempts: int,
    delay_seconds: float,
    runner: Runner | None = None,
    sleeper: Sleeper = time.sleep,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    if not command:
        raise ValueError("command must not be empty")
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    if delay_seconds < 0:
        raise ValueError("delay_seconds must be non-negative")

    resolved_runner = runner or _run_command
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    joined_command = shlex.join(command)
    final_exit_code = 1

    for attempt in range(1, attempts + 1):
        print(
            f"ci.docker_build attempt {attempt}/{attempts} starting: {joined_command}",
            file=err,
            flush=True,
        )
        result = resolved_runner(command)
        _write_stream(out, result.stdout)
        _write_stream(err, result.stderr)

        final_exit_code = result.returncode
        if result.returncode == 0:
            print(f"ci.docker_build attempt {attempt}/{attempts} succeeded", file=err, flush=True)
            return 0

        print(
            f"ci.docker_build attempt {attempt}/{attempts} failed with exit code "
            f"{result.returncode}",
            file=err,
            flush=True,
        )
        if attempt < attempts:
            print(
                f"ci.docker_build waiting {delay_seconds:g}s before retry",
                file=err,
                flush=True,
            )
            sleeper(delay_seconds)

    print(
        f"ci.docker_build exhausted {attempts} attempts; final exit code {final_exit_code}",
        file=err,
        flush=True,
    )
    return final_exit_code


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    return run_with_retries(
        args.command,
        attempts=args.attempts,
        delay_seconds=args.delay_seconds,
    )


def _run_command(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=False, text=True)


def _write_stream(stream: TextIO, text: str | None) -> None:
    if not text:
        return
    stream.write(text)
    if not text.endswith("\n"):
        stream.write("\n")
    stream.flush()


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a CI Docker build command with bounded, logged retries.",
    )
    parser.add_argument("--attempts", type=_positive_int, default=3)
    parser.add_argument("--delay-seconds", type=_non_negative_float, default=10.0)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.command[:1] == ["--"]:
        args.command = args.command[1:]
    if not args.command:
        parser.error("a command is required after --")
    return args


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _non_negative_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
