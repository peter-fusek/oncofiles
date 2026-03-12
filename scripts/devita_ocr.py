"""Extract text from VitalSource screenshots using Claude Vision OCR.

Usage:
    # OCR a single screenshot:
    uv run python scripts/devita_ocr.py ocr data/devita-ch40/page_618.png

    # OCR all screenshots in directory:
    uv run python scripts/devita_ocr.py ocr-all data/devita-ch40/

    # Combine all text files into a single chapter file:
    uv run python scripts/devita_ocr.py combine data/devita-ch40/
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import anthropic

OCR_MODEL = "claude-haiku-4-5-20251001"

OCR_SYSTEM_PROMPT = (
    "You are an OCR assistant for a medical textbook (DeVita Cancer 12th Edition). "
    "Extract ALL text from this page screenshot exactly as written. "
    "Preserve formatting, section headers, paragraph breaks, and superscript reference numbers. "
    "For tables, use markdown table format. "
    "Do not interpret, summarize, or add commentary. Output only the extracted text."
)


def ocr_image(image_path: Path) -> str:
    """OCR a single image file using Claude Vision."""
    client = anthropic.Anthropic()
    image_data = image_path.read_bytes()
    image_b64 = base64.b64encode(image_data).decode("utf-8")

    suffix = image_path.suffix.lower()
    media_type = "image/png" if suffix == ".png" else "image/jpeg"

    response = client.messages.create(
        model=OCR_MODEL,
        max_tokens=8192,
        system=OCR_SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": image_b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": "Extract all text from this textbook page.",
                    },
                ],
            }
        ],
    )

    return response.content[0].text if response.content else ""


def ocr_single(image_path: str) -> None:
    """OCR a single image and save text alongside it."""
    path = Path(image_path)
    if not path.exists():
        print(f"File not found: {path}")
        sys.exit(1)

    print(f"OCR: {path.name} ...", end=" ", flush=True)
    text = ocr_image(path)
    out_path = path.with_suffix(".txt")
    out_path.write_text(text, encoding="utf-8")
    print(f"→ {out_path.name} ({len(text)} chars)")


def ocr_all(directory: str) -> None:
    """OCR all PNG/JPEG screenshots in a directory."""
    dir_path = Path(directory)
    images = sorted(
        [f for f in dir_path.iterdir() if f.suffix.lower() in (".png", ".jpg", ".jpeg")],
        key=lambda f: f.name,
    )

    if not images:
        print(f"No image files found in {dir_path}")
        sys.exit(1)

    print(f"Found {len(images)} images in {dir_path}")
    for img in images:
        txt_path = img.with_suffix(".txt")
        if txt_path.exists():
            print(f"  SKIP {img.name} (already OCR'd)")
            continue
        print(f"  OCR: {img.name} ...", end=" ", flush=True)
        try:
            text = ocr_image(img)
        except Exception as e:
            print(f"FAILED: {e}")
            continue
        txt_path.write_text(text, encoding="utf-8")
        print(f"→ {len(text)} chars")

    print("Done!")


def combine(directory: str) -> None:
    """Combine all text files into a single chapter markdown file."""
    import re

    dir_path = Path(directory)
    txt_files = sorted(
        [f for f in dir_path.iterdir() if f.suffix == ".txt" and f.stem.startswith("pages_")],
        key=lambda f: (
            int(re.match(r"pages_(\d+)", f.stem).group(1)),
            int(re.search(r"_p(\d+)$", f.stem).group(1)),
        ),
    )

    if not txt_files:
        print(f"No page text files found in {dir_path}")
        sys.exit(1)

    output = ["# DeVita Chapter 40: Cancer of the Colon\n"]
    output.append(
        "**Source**: DeVita, Hellman, and Rosenberg's Cancer:"
        " Principles & Practice of Oncology, 12th Edition\n"
    )
    output.append("**Pages**: 618–677\n")
    output.append(
        "**Authors**: Steven K. Libutti, Leonard B. Saltz,"
        " Christopher G. Willett, Rebecca A. Levine\n\n---\n"
    )

    current_range = None
    for txt_file in txt_files:
        m = re.match(r"pages_(\d+-\d+)_p(\d+)", txt_file.stem)
        page_range = m.group(1)
        if page_range != current_range:
            output.append(f"\n<!-- Pages {page_range} -->\n")
            current_range = page_range
        text = txt_file.read_text(encoding="utf-8").strip()
        output.append(text)
        output.append("\n")

    combined_path = dir_path / "chapter_40_full.md"
    combined_path.write_text("\n".join(output), encoding="utf-8")
    print(f"Combined {len(txt_files)} pages → {combined_path}")
    print(f"Total size: {combined_path.stat().st_size:,} bytes")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "ocr":
        ocr_single(sys.argv[2])
    elif cmd == "ocr-all":
        ocr_all(sys.argv[2])
    elif cmd == "combine":
        combine(sys.argv[2])
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)
