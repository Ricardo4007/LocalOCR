"""Run local PaddleOCR over a directory of document photos."""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODEL_CACHE = PROJECT_ROOT / "models" / "paddlex"

# PaddleX otherwise defaults to %USERPROFILE%\.paddlex. Set these before
# importing PaddleOCR because PaddleX reads them during module initialization.
os.environ.setdefault("PADDLE_PDX_CACHE_HOME", str(MODEL_CACHE))
os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Batch OCR document photos with the local PaddleOCR pipeline."
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("input"),
        help="Directory or one PNG/JPG/JPEG image (default: input).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("output"),
        help="Directory for OCR results (default: output).",
    )
    parser.add_argument(
        "--device",
        default="auto",
        help="auto, cpu, or a Paddle device such as gpu:0 (default: auto).",
    )
    parser.add_argument(
        "--doc-orientation",
        action="store_true",
        help="Enable document orientation classification.",
    )
    parser.add_argument(
        "--unwarp",
        action="store_true",
        help="Enable document perspective correction.",
    )
    parser.add_argument(
        "--line-orientation",
        action="store_true",
        help="Enable text-line orientation classification.",
    )
    parser.add_argument(
        "--info",
        action="store_true",
        help="Print Paddle/PaddleOCR and device information, then exit.",
    )
    parser.add_argument(
        "--cpu-threads",
        type=int,
        default=0,
        help="Total CPU inference threads; 0 uses all logical CPUs (default: 0).",
    )
    parser.add_argument(
        "--include-json",
        action="store_true",
        help="Also save detailed JSON results (disabled by default).",
    )
    return parser.parse_args()


def load_runtime() -> tuple[Any, Any]:
    try:
        import paddle
        from paddleocr import PaddleOCR
    except ImportError as exc:
        raise RuntimeError(
            "PaddleOCR is not installed. Run `uv sync` from the project directory."
        ) from exc
    return paddle, PaddleOCR


def choose_device(paddle: Any, requested: str) -> str:
    if requested != "auto":
        return requested
    try:
        if paddle.device.is_compiled_with_cuda() and paddle.device.cuda.device_count() > 0:
            return "gpu:0"
    except (AttributeError, RuntimeError):
        pass
    return "cpu"


def json_safe(value: Any) -> Any:
    """Convert common Paddle/Numpy values into JSON-compatible values."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if hasattr(value, "tolist"):
        return json_safe(value.tolist())
    return str(value)


def result_to_dict(result: Any) -> dict[str, Any]:
    raw = getattr(result, "json", None)
    if callable(raw):
        raw = raw()
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = {"raw_json": raw}
    if raw is None and isinstance(result, dict):
        raw = result
    if raw is None:
        raw = {"result_repr": repr(result)}
    return json_safe(raw)


def unwrap_result(data: dict[str, Any]) -> dict[str, Any]:
    nested = data.get("res")
    return nested if isinstance(nested, dict) else data


def first_list(data: dict[str, Any], *keys: str) -> list[Any]:
    for key in keys:
        value = data.get(key)
        if isinstance(value, list):
            return value
    return []


def extract_text(data: dict[str, Any]) -> tuple[list[str], list[float | None]]:
    payload = unwrap_result(data)
    texts = first_list(payload, "rec_texts", "texts", "text")
    scores = first_list(payload, "rec_scores", "scores", "confidence")
    normalized_texts = [str(text) for text in texts]
    normalized_scores: list[float | None] = []
    for score in scores:
        try:
            normalized_scores.append(float(score))
        except (TypeError, ValueError):
            normalized_scores.append(None)
    if len(normalized_scores) < len(normalized_texts):
        normalized_scores.extend([None] * (len(normalized_texts) - len(normalized_scores)))
    return normalized_texts, normalized_scores


def average_score(scores: list[float | None]) -> str:
    values = [score for score in scores if score is not None]
    return f"{sum(values) / len(values):.6f}" if values else ""


def build_ocr(
    PaddleOCR: Any, device: str, args: argparse.Namespace, cpu_threads: int
) -> Any:
    options: dict[str, Any] = {
        "lang": "ch",
        "device": device,
        "use_doc_orientation_classify": args.doc_orientation,
        "use_doc_unwarping": args.unwarp,
        "use_textline_orientation": args.line_orientation,
    }
    if device.startswith("cpu"):
        options["cpu_threads"] = cpu_threads
    return PaddleOCR(**options)


def load_fixed_size_image(image_path: Path) -> Any:
    """Normalize image shape to avoid a Windows CPU MKL-DNN shape-cache fault."""
    try:
        import numpy as np
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError(
            "Image preprocessing requires Pillow and NumPy. Run `uv sync`."
        ) from exc

    target_width, target_height = 2300, 3300
    with Image.open(image_path) as source:
        image = source.convert("RGB")
        image.thumbnail((target_width, target_height), Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", (target_width, target_height), "white")
        canvas.paste(
            image,
            ((target_width - image.width) // 2, (target_height - image.height) // 2),
        )
        return np.asarray(canvas)


def process_image(
    ocr: Any, image_path: Path, output: Path, device: str, include_json: bool
) -> dict[str, str]:
    results = ocr.predict(input=load_fixed_size_image(image_path))
    all_texts: list[str] = []
    all_scores: list[float | None] = []
    raw_results: list[dict[str, Any]] = []

    for result in results:
        raw = result_to_dict(result)
        texts, scores = extract_text(raw)
        all_texts.extend(texts)
        all_scores.extend(scores)
        raw_results.append(raw)
        save_image = getattr(result, "save_to_img", None)
        if callable(save_image):
            save_image(str(output / f"{image_path.stem}_ocr_res_img.png"))

    text_path = output / f"{image_path.stem}.txt"
    text_path.write_text("\n".join(all_texts) + "\n", encoding="utf-8")
    json_name = ""
    if include_json:
        json_path = output / f"{image_path.stem}.json"
        json_path.write_text(
            json.dumps(
                {
                    "image": image_path.name,
                    "device": device,
                    "texts": all_texts,
                    "scores": all_scores,
                    "raw_results": raw_results,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        json_name = json_path.name
    return {
        "image": image_path.name,
        "device": device,
        "lines": str(len(all_texts)),
        "characters": str(sum(len(text) for text in all_texts)),
        "average_score": average_score(all_scores),
        "text_file": text_path.name,
        "json_file": json_name,
        "error": "",
    }


def write_summary(output: Path, rows: list[dict[str, str]]) -> None:
    if not rows:
        return
    summary_path = output / "summary.csv"
    with summary_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def input_images(input_path: Path) -> list[Path]:
    if input_path.is_file():
        return [input_path] if input_path.suffix.lower() in {".png", ".jpg", ".jpeg"} else []
    supported_suffixes = {".png", ".jpg", ".jpeg"}
    return sorted(
        path
        for path in input_path.iterdir()
        if path.is_file() and path.suffix.lower() in supported_suffixes
    )


def run(
    args: argparse.Namespace,
    progress: Callable[[int, int, str], None] | None = None,
) -> int:
    if args.cpu_threads < 0:
        print("--cpu-threads cannot be negative.", file=sys.stderr)
        return 2

    if args.info:
        paddle, _ = load_runtime()
        device = choose_device(paddle, args.device)
        print(f"PaddlePaddle: {getattr(paddle, '__version__', 'unknown')}")
        try:
            import paddleocr

            print(f"PaddleOCR: {getattr(paddleocr, '__version__', 'unknown')}")
        except ImportError:
            pass
        print(f"CUDA compiled: {paddle.device.is_compiled_with_cuda()}")
        print(f"Selected device: {device}")
        return 0

    if not args.input.exists():
        print(f"Input path does not exist: {args.input}", file=sys.stderr)
        return 2

    images = input_images(args.input)
    if not images:
        print(f"No PNG/JPG/JPEG files found in {args.input}", file=sys.stderr)
        return 2

    args.output.mkdir(parents=True, exist_ok=True)
    paddle, PaddleOCR = load_runtime()
    device = choose_device(paddle, args.device)
    total_cpu_threads = args.cpu_threads or (os.cpu_count() or 1)

    summary_rows: list[dict[str, str]] = []
    print(f"Single OCR pipeline: {total_cpu_threads} CPU thread(s)")
    if progress:
        progress(0, len(images), "OCR 模型初始化中")
    ocr = build_ocr(PaddleOCR, device, args, total_cpu_threads)
    if progress:
        progress(0, len(images), "OCR 模型已就绪")
    for index, image_path in enumerate(images, start=1):
        print(f"[{index}/{len(images)}] {image_path.name}")
        if progress:
            progress(index - 1, len(images), f"正在识别：{image_path.name}")
        try:
            summary_rows.append(
                process_image(ocr, image_path, args.output, device, args.include_json)
            )
        except Exception as exc:
            print(f"Error processing {image_path.name}: {type(exc).__name__}: {exc}", file=sys.stderr)
            summary_rows.append(
                {
                    "image": image_path.name,
                    "device": device,
                    "lines": "",
                    "characters": "",
                    "average_score": "",
                    "text_file": "",
                    "json_file": "",
                    "error": f"{type(exc).__name__}: see local console log",
                }
            )
        if progress:
            progress(index, len(images), f"已完成：{image_path.name}")

    write_summary(args.output, summary_rows)
    failed = [row for row in summary_rows if row.get("error")]
    if failed:
        print(
            f"Completed {len(images)} image(s) with {len(failed)} failure(s). "
            f"Results: {args.output}",
            file=sys.stderr,
        )
        return 1
    print(f"Completed {len(images)} image(s). Results: {args.output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(run(parse_args()))
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
