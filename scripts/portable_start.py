"""Start the portable console with all default writable paths inside the app."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def configure_environment() -> None:
    paths = {
        "PADDLE_PDX_CACHE_HOME": ROOT / "models" / "paddlex",
        "PADDLE_HOME": ROOT / "models" / "paddle",
        "HF_HOME": ROOT / "models" / "huggingface",
        "MODELSCOPE_CACHE": ROOT / "models" / "modelscope",
        "XDG_CACHE_HOME": ROOT / "models" / "cache",
        "MPLCONFIGDIR": ROOT / "models" / "matplotlib",
        "TEMP": ROOT / "runtime-tmp",
        "TMP": ROOT / "runtime-tmp",
        "HOME": ROOT / "runtime-home",
        "USERPROFILE": ROOT / "runtime-home",
        "TCL_LIBRARY": ROOT / "runtime" / "tcl" / "tcl8.6",
        "TK_LIBRARY": ROOT / "runtime" / "tcl" / "tk8.6",
    }
    for key, path in paths.items():
        os.environ[key] = str(path)
    for key in ("TEMP", "HOME", "MPLCONFIGDIR"):
        paths[key].mkdir(parents=True, exist_ok=True)
    os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    sys.dont_write_bytecode = True
    os.chdir(ROOT)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    configure_environment()
    log_path = ROOT / "runtime-tmp" / "console.log"
    with log_path.open("a", encoding="utf-8", buffering=1) as log:
        sys.stdout = log
        sys.stderr = log
        try:
            sys.path.insert(0, str(Path(__file__).parent))
            import web_console

            web_console.main(["--port", "0"] + (["--no-browser"] if args.no_browser else []))
        except Exception:
            import traceback

            traceback.print_exc()
            if not args.no_browser:
                import ctypes

                ctypes.windll.user32.MessageBoxW(
                    0, f"Local OCR could not start. See {log_path}", "Local OCR", 16
                )
            raise


if __name__ == "__main__":
    main()
