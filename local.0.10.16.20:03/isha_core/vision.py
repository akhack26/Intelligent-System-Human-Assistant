"""Vision: honest capability reporting + best-effort local inference.

Two independent paths:
1. A local multimodal GGUF (llava / bakllava / moondream / minicpm-v /
   qwen2-vl ...) + its ``mmproj`` projector, run through llama-cpp-python's
   chat handlers. Requires the *native* llama-cpp-python backend.
2. OCR via Tesseract (pytesseract + the tesseract binary) for "read this
   image" - no model needed.

``status()`` says exactly which of these is usable. Nothing pretends to see.
"""
from __future__ import annotations

import base64
import mimetypes
import shutil
from pathlib import Path


def ocr_available() -> tuple:
    try:
        import pytesseract  # noqa: F401
    except Exception:
        return False, "pytesseract not installed (pip install pytesseract)"
    if not shutil.which("tesseract"):
        return False, "tesseract binary not found (install Tesseract OCR)"
    try:
        from PIL import Image  # noqa: F401
    except Exception:
        return False, "Pillow not installed"
    return True, ""


def _handler_for(model_name: str):
    n = model_name.lower()
    try:
        from llama_cpp import llama_chat_format as cf
    except Exception:
        return None
    table = [("moondream", "MoondreamChatHandler"), ("minicpm", "MiniCPMv26ChatHandler"),
             ("qwen2.5-vl", "Qwen25VLChatHandler"), ("qwen2-vl", "Qwen25VLChatHandler"),
             ("llava-v1.6", "Llava16ChatHandler"), ("llava-1.6", "Llava16ChatHandler"),
             ("nanollava", "NanoLlavaChatHandler"), ("llama3-vision-alpha", "Llama3VisionAlphaChatHandler")]
    for key, cls in table:
        if key in n and hasattr(cf, cls):
            return getattr(cf, cls)
    return getattr(cf, "Llava15ChatHandler", None)


def status(model_path: Path | None, mmproj_path: Path | None, backend_mode: str) -> dict:
    ocr_ok, ocr_why = ocr_available()
    model_ok, why = False, ""
    if model_path is None:
        why = "no vision model configured (models.vision)"
    elif mmproj_path is None:
        why = f"'{model_path.name}' found but its mmproj projector file is missing"
    elif backend_mode != "native":
        why = "vision needs the native llama-cpp-python backend (current backend: %s)" % backend_mode
    elif _handler_for(model_path.name) is None:
        why = "installed llama-cpp-python has no multimodal chat handler"
    else:
        model_ok = True
    if model_ok:
        state = "READY"
    elif ocr_ok:
        state = "DEGRADED"
    else:
        state = "UNAVAILABLE"
    return {"state": state, "model": model_ok, "model_reason": why, "ocr": ocr_ok, "ocr_reason": ocr_why}


def _data_uri(image_path: Path) -> str:
    mime = mimetypes.guess_type(str(image_path))[0] or "image/png"
    return f"data:{mime};base64," + base64.b64encode(Path(image_path).read_bytes()).decode("ascii")


def describe_with_model(image_path: Path, prompt: str, model_path: Path, mmproj_path: Path,
                        n_ctx: int = 4096, n_threads: int | None = None, max_tokens: int = 400) -> str:
    from llama_cpp import Llama
    handler_cls = _handler_for(model_path.name)
    if handler_cls is None:
        raise RuntimeError("no multimodal chat handler available")
    handler = handler_cls(clip_model_path=str(mmproj_path), verbose=False)
    llm = Llama(model_path=str(model_path), chat_handler=handler, n_ctx=n_ctx,
                n_threads=n_threads, verbose=False)
    try:
        out = llm.create_chat_completion(messages=[{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": _data_uri(image_path)}}]}],
            max_tokens=max_tokens, temperature=0.2)
        return (out["choices"][0]["message"].get("content") or "").strip()
    finally:
        del llm


def ocr(image_path: Path, lang: str = "eng") -> str:
    import pytesseract
    from PIL import Image
    langs = lang
    try:
        if "hin" in pytesseract.get_languages(config=""):
            langs = "eng+hin"
    except Exception:
        pass
    return pytesseract.image_to_string(Image.open(image_path), lang=langs).strip()
