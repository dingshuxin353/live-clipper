"""Correct ASR transcripts using the cheap model and a maintained glossary."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .models import CorrectedTranscript, GlossaryTerm
from .prompt_loader import load_prompt
from .transcribe import transcript_sentences_from_raw
from .utils import read_json, write_failure_log, write_json


DEFAULT_CORRECTION_BATCH_SIZE = 80


def emit_progress(message: str) -> None:
    print(message, flush=True)


def correction_checkpoint_path(output_path: Path) -> Path:
    return output_path.with_name(f"{output_path.stem}.partial{output_path.suffix}")


def write_correction_checkpoint(
    checkpoint_path: Path,
    processed_sentence_count: int,
    corrected_sentence_items: list[dict[str, Any]],
    correction_items: list[dict[str, Any]],
) -> None:
    write_json(
        checkpoint_path,
        {
            "processed_sentence_count": processed_sentence_count,
            "sentences": corrected_sentence_items,
            "corrections": correction_items,
        },
    )


def load_correction_checkpoint(checkpoint_path: Path) -> tuple[int, list[dict[str, Any]], list[dict[str, Any]]]:
    checkpoint = read_json(checkpoint_path)
    return (
        int(checkpoint.get("processed_sentence_count", 0)),
        list(checkpoint.get("sentences", [])),
        list(checkpoint.get("corrections", [])),
    )


def load_glossary(path: Path) -> list[GlossaryTerm]:
    if not path.exists():
        return []
    return [GlossaryTerm.model_validate(item) for item in read_json(path)]


def iter_batches(items: list[Any], batch_size: int):
    for index in range(0, len(items), batch_size):
        yield index, items[index:index + batch_size]


def log_validation_failure(
    sentences_count: int,
    corrected_sentences_count: int | None,
    payload: dict[str, Any],
    corrected_payload: dict[str, Any],
    batch_start: int,
) -> None:
    write_failure_log(
        "correct_transcript_validation_failure",
        {
            "raw_sentence_count": sentences_count,
            "model_sentence_count": corrected_sentences_count,
            "batch_start": batch_start,
            "user_payload": payload,
            "model_response": corrected_payload,
        },
    )


def correct_transcript_file(
    raw_transcript_path: Path,
    glossary_path: Path,
    output_path: Path,
    client: Any,
    batch_size: int = DEFAULT_CORRECTION_BATCH_SIZE,
    *,
    resume: bool = False,
    prompt_dir: Path | None = None,
) -> CorrectedTranscript:
    if batch_size <= 0:
        raise ValueError("Transcript correction batch_size must be greater than 0")

    raw = read_json(raw_transcript_path)
    sentences = transcript_sentences_from_raw(raw)
    glossary = load_glossary(glossary_path)
    system_prompt = load_prompt("cheap_correct_transcript.md", "cheap transcript correction prompt", prompt_dir=prompt_dir)
    glossary_payload = [term.model_dump() for term in glossary]
    checkpoint_path = correction_checkpoint_path(output_path)
    if resume and checkpoint_path.exists():
        processed_sentence_count, corrected_sentence_items, correction_items = load_correction_checkpoint(checkpoint_path)
    else:
        processed_sentence_count = 0
        corrected_sentence_items = []
        correction_items = []

    resume_sentence_count = processed_sentence_count
    total_sentences = len(sentences)
    emit_progress(
        f"[ASR校对] 开始: 已处理 {resume_sentence_count}/{total_sentences} 句"
    )
    for batch_start, batch_sentences in iter_batches(sentences[resume_sentence_count:], batch_size):
        absolute_batch_start = resume_sentence_count + batch_start
        batch_end = absolute_batch_start + len(batch_sentences)
        emit_progress(
            f"[ASR校对] 第 {absolute_batch_start + 1}-{batch_end}/{total_sentences} 句: 正在请求 Agnes"
        )
        payload = {
            "sentences": [sentence.model_dump() for sentence in batch_sentences],
            "glossary": glossary_payload,
        }

        corrected_payload = client.complete_json(system_prompt, payload, max_tokens=8192)
        if isinstance(corrected_payload, list):
            corrected_payload = {"sentences": corrected_payload, "corrections": []}
        corrected_sentences = corrected_payload.get("sentences", [])
        if not isinstance(corrected_sentences, list):
            log_validation_failure(len(batch_sentences), None, payload, corrected_payload, absolute_batch_start)
            raise ValueError("Corrected transcript sentences must be a list")
        if len(corrected_sentences) != len(batch_sentences):
            log_validation_failure(
                len(batch_sentences),
                len(corrected_sentences),
                payload,
                corrected_payload,
                absolute_batch_start,
            )
            corrected_sentences = [sentence.model_dump() for sentence in batch_sentences]
            corrected_payload["corrections"] = []
            emit_progress(
                f"[ASR校对] 第 {absolute_batch_start + 1}-{batch_end}/{total_sentences} 句: "
                "Agnes 返回句数不一致, 已保留原始ASR文本"
            )
        corrected_sentence_items.extend([
            {
                **item,
                "start": original.start,
                "end": original.end,
                "speaker": original.speaker,
            }
            for original, item in zip(batch_sentences, corrected_sentences, strict=True)
        ])
        correction_items.extend(corrected_payload.get("corrections", []))
        processed_sentence_count = absolute_batch_start + len(batch_sentences)
        write_correction_checkpoint(
            checkpoint_path,
            processed_sentence_count,
            corrected_sentence_items,
            correction_items,
        )
        emit_progress(f"[ASR校对] 已写入断点: {processed_sentence_count}/{total_sentences} 句")

    corrected_payload = {
        "sentences": corrected_sentence_items,
        "corrections": correction_items,
    }
    try:
        corrected = CorrectedTranscript.model_validate(corrected_payload)
    except ValidationError:
        write_failure_log(
            "correct_transcript_validation_failure",
            {
                "raw_sentence_count": len(sentences),
                "model_sentence_count": len(corrected_sentence_items),
                "user_payload": {
                    "sentences": [sentence.model_dump() for sentence in sentences],
                    "glossary": glossary_payload,
                },
                "model_response": corrected_payload,
            },
        )
        raise
    write_json(output_path, corrected.model_dump())
    checkpoint_path.unlink(missing_ok=True)
    emit_progress(f"[ASR校对] 全部完成: {len(corrected.sentences)} 句 -> {output_path}")
    return corrected
