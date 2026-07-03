"""
builder.py  —  Content loader and presentation builder.

Loads a JSON or YAML content file and delegates each slide
to the appropriate renderer via renderer.RENDERERS.
"""
from __future__ import annotations
import json, os, sys
from pptx import Presentation

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from theme    import SW, SH
from renderer import RENDERERS


# ── Loaders ───────────────────────────────────────────────────────────────────

def load_content(path: str) -> dict:
    """
    Load a JSON or YAML content file.
    Returns the parsed dict (must contain a 'slides' list).
    Raises ImportError if PyYAML is needed but not installed.
    """
    path = str(path)
    with open(path, "r", encoding="utf-8") as f:
        if path.lower().endswith((".yaml", ".yml")):
            try:
                import yaml
                return yaml.safe_load(f)
            except ImportError:
                raise ImportError(
                    "PyYAML is required to load YAML content files.\n"
                    "Install it with:  pip install PyYAML"
                )
        else:
            return json.load(f)


# ── Builder ───────────────────────────────────────────────────────────────────

def build_presentation(slides: list, output_path: str) -> None:
    """
    Render a list of slide-definition dicts into a PPTX file.

    Each dict must have a 'type' key matching one of the supported
    renderer types.  See renderer.RENDERERS for the full list.
    """
    prs              = Presentation()
    prs.slide_width  = SW
    prs.slide_height = SH

    supported = ", ".join(sorted(RENDERERS.keys()))
    for i, sd in enumerate(slides, 1):
        stype    = sd.get("type", "bullets")
        renderer = RENDERERS.get(stype)
        if renderer is None:
            raise ValueError(
                f"Slide {i}: unknown type '{stype}'.\n"
                f"Supported types: {supported}"
            )
        renderer(prs, sd)

    prs.save(output_path)
    print(f"✅  Saved: {output_path}  ({len(prs.slides)} slides)")


def build_from_file(content_path: str, output: str = None) -> None:
    """
    High-level entry point: load a content file and build the PPTX.

    Parameters
    ----------
    content_path : path to a JSON or YAML content file
    output       : optional output path (overrides the 'output' key in the file)
    """
    data     = load_content(content_path)
    out_path = output or data.get("output", "presentation.pptx")

    # Resolve relative paths against CWD
    if not os.path.isabs(out_path):
        out_path = os.path.join(os.getcwd(), out_path)

    slides = data.get("slides", [])
    if not slides:
        raise ValueError(
            f"No slides found in '{content_path}'.\n"
            "Make sure the file contains a top-level 'slides' list."
        )

    print(f"  Content : {content_path}")
    print(f"  Slides  : {len(slides)}")
    print(f"  Output  : {out_path}")
    build_presentation(slides, out_path)
