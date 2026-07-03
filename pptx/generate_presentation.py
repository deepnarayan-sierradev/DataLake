#!/usr/bin/env python3
"""
generate_presentation.py  —  CLI entry point for the generic PPTX generator.

Usage
-----
  # Use the default data-lake content file
  python ./pptx/generate_presentation.py

  # Use a custom JSON or YAML content file
  python ./pptx/generate_presentation.py path/to/my_slides.json
  python ./pptx/generate_presentation.py path/to/my_slides.yaml

  # Override the output filename
  python ./pptx/generate_presentation.py my_slides.yaml --output MyDeck.pptx

  # List all supported slide types
  python ./pptx/generate_presentation.py --list-types

Content file format
-------------------
  output: MyPresentation.pptx    # optional output filename
  slides:
    - type: title
      title: "My Title"
      ...
    - type: bullets
      title: "Key Points"
      left:
        heading: "Section A"
        items:
          - "Point one"
          - "Point two"
      ...

Supported slide types
---------------------
  title        bullets      cards       table
  metrics      flow         split       swimlanes
  checklist    features     architecture

Run  --list-types  for a full list with brief descriptions.

Dependencies
------------
  pip install python-pptx Pillow PyYAML
"""
import sys, os, argparse

# Ensure the pptx/ directory is on the path regardless of how the script is invoked
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from builder  import build_from_file
from renderer import RENDERERS

_DEFAULT_CONTENT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "content", "enterprise_datalake.yaml",
)

_TYPE_DESCRIPTIONS = {
    "title":        "Big title, tagline, status badges, date",
    "bullets":      "1- or 2-column bullet/numbered list + optional code block",
    "cards":        "Row of N auto-sized coloured cards (connector cards, overview cards, tech stack)",
    "table":        "Column headers + data rows + optional callout + before→after pairs",
    "metrics":      "Row of big-number metric boxes + optional detail table",
    "flow":         "Source boxes → golden-record output (entity resolution)",
    "split":        "Left bullet list + right syntax-highlighted code/query panel",
    "swimlanes":    "N vertical lane columns (roadmap, planning)",
    "checklist":    "Icon checklist for closing/summary slides",
    "features":     "Two-column bar+title+body layout (observability, reliability)",
    "architecture": "Full architecture diagram: top rows + layer boxes + lambda boxes",
}


def main():
    parser = argparse.ArgumentParser(
        prog="generate_presentation.py",
        description="Generic PowerPoint generator — theme, fonts, and colours are fixed; "
                    "provide your own JSON or YAML content file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "content", nargs="?", default=_DEFAULT_CONTENT,
        metavar="CONTENT_FILE",
        help="Path to a JSON or YAML content file. "
             f"Defaults to: pptx/content/enterprise_datalake.yaml",
    )
    parser.add_argument(
        "--output", "-o", metavar="OUTPUT.pptx",
        help="Output PPTX filename (overrides the 'output' key in the content file).",
    )
    parser.add_argument(
        "--list-types", action="store_true",
        help="Print all supported slide types and exit.",
    )

    args = parser.parse_args()

    if args.list_types:
        print("\nSupported slide types\n" + "─" * 40)
        for t in sorted(RENDERERS.keys()):
            desc = _TYPE_DESCRIPTIONS.get(t, "")
            print(f"  {t:<16}  {desc}")
        print()
        return

    if not os.path.exists(args.content):
        parser.error(f"Content file not found: {args.content}")

    try:
        build_from_file(args.content, output=args.output)
    except (ValueError, ImportError) as exc:
        print(f"\n❌  Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
