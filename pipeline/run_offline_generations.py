"""Run the LLM-only phase of an offline evaluation bundle.

This script needs the public bundle files and OpenAI API access. It never imports
the PostgreSQL helpers and does not need database connectivity.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from _offline_eval import (
    GENERATIONS_NAME,
    read_jsonl,
    verify_public_bundle,
)

TRANSIENT_ERROR_NAMES = {
    "RateLimitError",
    "APIConnectionError",
    "APITimeoutError",
    "InternalServerError",
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00",
        "Z",
    )


def extract_sql(text: str) -> str:
    text = text.strip()
    fence = re.match(r"^```(?:sql)?\s*\n(.*?)\n?```$", text, re.DOTALL)
    return fence.group(1).strip() if fence else text


def nested_attr(value, *names, default=0):
    for name in names:
        if value is None:
            return default
        value = getattr(value, name, None)
    return default if value is None else value


def usage_dict(usage) -> dict | None:
    if usage is None:
        return None
    return {
        "input_tokens": nested_attr(usage, "input_tokens"),
        "cached_tokens": nested_attr(
            usage,
            "input_tokens_details",
            "cached_tokens",
        ),
        "output_tokens": nested_attr(usage, "output_tokens"),
        "reasoning_tokens": nested_attr(
            usage,
            "output_tokens_details",
            "reasoning_tokens",
        ),
        "total_tokens": nested_attr(usage, "total_tokens"),
    }


def is_reasoning_model(model: str) -> bool:
    model = (model or "").lower()
    return model.startswith("gpt-5") or (
        len(model) >= 2 and model[0] == "o" and model[1].isdigit()
    )


def append_record(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def load_done(
    path: Path,
    *,
    model: str,
    effort: str,
) -> set[tuple[str, str]]:
    if not path.exists():
        return set()
    done: set[tuple[str, str]] = set()
    for record in read_jsonl(path):
        if record.get("model") == model and record.get("effort") == effort:
            request_id = record.get("request_id")
            request_sha256 = record.get("request_sha256")
            if request_id and request_sha256:
                done.add((request_id, request_sha256))
    return done


def contiguous_groups(requests: list[dict]) -> list[list[dict]]:
    groups: list[list[dict]] = []
    key = None
    for request in requests:
        request_key = (request["schema_sha256"], request["db_id"])
        if request_key != key:
            groups.append([])
            key = request_key
        groups[-1].append(request)
    return groups


async def run_one(client, semaphore, request, model, effort, output_path):
    import openai

    async with semaphore:
        record = {
            "request_id": request["request_id"],
            "request_sha256": request["request_sha256"],
            "model": model,
            "effort": effort,
            "recorded_at_utc": utc_now_iso(),
        }
        kwargs = {
            "model": model,
            "instructions": request["instructions"],
            "input": request["input"],
        }
        if is_reasoning_model(model):
            kwargs["reasoning"] = {"effort": effort}
        started = time.monotonic()
        try:
            response = await client.responses.create(**kwargs)
            record["generated_sql"] = extract_sql(response.output_text)
            record["usage"] = usage_dict(response.usage)
        except Exception as exc:
            if exc.__class__.__name__ in TRANSIENT_ERROR_NAMES or isinstance(
                exc,
                (
                    openai.RateLimitError,
                    openai.APIConnectionError,
                    openai.APITimeoutError,
                    openai.InternalServerError,
                ),
            ):
                print(
                    f"transient error on {request['request_id']}; "
                    f"leaving it pending: {exc}",
                    flush=True,
                )
                return None
            record["generated_sql"] = None
            record["error"] = f"llm_call_failed: {exc}"
        record["latency_sec"] = round(time.monotonic() - started, 3)
        append_record(output_path, record)
        return record


async def run(args: argparse.Namespace) -> None:
    from dotenv import load_dotenv
    from openai import AsyncOpenAI

    load_dotenv()
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY not set.")

    manifest, requests = verify_public_bundle(args.bundle_dir)
    output_path = args.output or args.bundle_dir / GENERATIONS_NAME
    done = load_done(output_path, model=args.model, effort=args.effort)
    pending = [
        request
        for request in requests
        if (request["request_id"], request["request_sha256"]) not in done
    ]
    print(
        f"{manifest['eval_name']}/{manifest.get('split', 'test')}: "
        f"{len(requests)} requests, {len(done)} done, {len(pending)} pending"
    )
    if not pending:
        return

    client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])
    semaphore = asyncio.Semaphore(args.concurrency)
    completed = 0
    for group_number, group in enumerate(contiguous_groups(pending), 1):
        tasks = [
            run_one(
                client,
                semaphore,
                request,
                args.model,
                args.effort,
                output_path,
            )
            for request in group
        ]
        for task in asyncio.as_completed(tasks):
            if await task is not None:
                completed += 1
                if completed % 50 == 0:
                    print(f"{completed}/{len(pending)} generated", flush=True)
        print(
            f"[{group_number}] finished db_id={group[0]['db_id']} "
            f"({len(group)} requests)",
            flush=True,
        )
    print(f"Wrote {completed} generation record(s) to {output_path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the API-only generation phase for an offline eval bundle."
    )
    parser.add_argument("--bundle-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--model", default="gpt-5.5")
    parser.add_argument(
        "--effort",
        default="low",
        choices=["none", "minimal", "low", "medium", "high", "xhigh"],
    )
    parser.add_argument("--concurrency", type=int, default=3)
    args = parser.parse_args()
    if args.concurrency <= 0:
        raise SystemExit("--concurrency must be greater than zero.")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
