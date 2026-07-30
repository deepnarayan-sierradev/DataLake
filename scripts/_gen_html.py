"""Regenerate HTML from markdown for documentation files."""
import re
import pathlib

DOCS = ["PLATFORM_STATUS", "PIPELINE_FLOW", "DEVELOPER_GUIDE", "DEPLOYMENT_GUIDE"]
BASE = pathlib.Path(__file__).parent.parent / "docs"

CSS = """
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         max-width: 1100px; margin: 40px auto; padding: 0 24px;
         color: #24292e; line-height: 1.6; }
  h1,h2,h3,h4 { border-bottom: 1px solid #eee; padding-bottom: 8px; margin-top: 32px; }
  code { background: #f6f8fa; padding: 2px 6px; border-radius: 4px;
         font-family: monospace; font-size: 0.9em; }
  pre  { background: #f6f8fa; padding: 16px; border-radius: 6px; overflow-x: auto; }
  pre code { background: none; padding: 0; }
  table { border-collapse: collapse; width: 100%; margin: 16px 0; }
  td,th { border: 1px solid #dfe2e5; padding: 8px 13px; }
  tr:nth-child(even) { background: #f6f8fa; }
  blockquote { border-left: 4px solid #dfe2e5; margin: 0;
               padding: 8px 16px; color: #6a737d; }
  hr { border: none; border-top: 1px solid #eee; margin: 32px 0; }
  a { color: #0366d6; }
"""


def md_to_html(md: str) -> str:
    title = md.split("\n")[0].lstrip("# ").strip()
    body = md

    code_blocks: list[str] = []
    def save_block(m: re.Match) -> str:
        code_blocks.append(m.group(1))
        return f"\x00CODEBLOCK{len(code_blocks) - 1}\x00"
    body = re.sub(r"```[^\n]*\n(.*?)```", save_block, body, flags=re.DOTALL)

    body = re.sub(r"^#### (.+)$", r"<h4>\1</h4>", body, flags=re.MULTILINE)
    body = re.sub(r"^### (.+)$",  r"<h3>\1</h3>",  body, flags=re.MULTILINE)
    body = re.sub(r"^## (.+)$",   r"<h2>\1</h2>",   body, flags=re.MULTILINE)
    body = re.sub(r"^# (.+)$",    r"<h1>\1</h1>",    body, flags=re.MULTILINE)

    body = re.sub(r"`([^`]+)`", r"<code>\1</code>", body)

    body = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", body)
    body = re.sub(r"\*([^*]+)\*",   r"<em>\1</em>",         body)

    body = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', body)

    body = re.sub(r"^> (.+)$", r"<blockquote>\1</blockquote>", body, flags=re.MULTILINE)

    body = re.sub(r"^---$", "<hr/>", body, flags=re.MULTILINE)

    lines = body.split("\n")
    out: list[str] = []
    in_table = False
    for line in lines:
        if "|" in line and line.strip().startswith("|"):
            if not in_table:
                out.append("<table>")
                in_table = True
            if re.match(r"^\s*\|[\s\-:|]+\|\s*$", line):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            out.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
        else:
            if in_table:
                out.append("</table>")
                in_table = False
            out.append(line)
    if in_table:
        out.append("</table>")
    body = "\n".join(out)

    for i, code in enumerate(code_blocks):
        escaped = (code.replace("&", "&amp;").replace("<", "&lt;")
                       .replace(">", "&gt;"))
        body = body.replace(f"\x00CODEBLOCK{i}\x00",
                            f"<pre><code>{escaped}</code></pre>")

    body = re.sub(r"(?m)^- (.+)$", r"<li>\1</li>", body)
    body = re.sub(r"(<li>.*?</li>(\n<li>.*?</li>)*)", r"<ul>\1</ul>",
                  body, flags=re.DOTALL)

    return title, body


def build_html(title: str, body: str) -> str:
    return (
        "<!DOCTYPE html>\n"
        "<html lang=\"en\">\n"
        "<head>\n"
        "<meta charset=\"UTF-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">\n"
        f"<title>{title}</title>\n"
        f"<style>{CSS}</style>\n"
        "</head>\n"
        "<body>\n"
        f"{body}\n"
        "</body>\n"
        "</html>\n"
    )


for name in DOCS:
    md_path = BASE / f"{name}.md"
    html_path = BASE / f"{name}.html"
    md_text = md_path.read_text(encoding="utf-8")
    title, body = md_to_html(md_text)
    html_path.write_text(build_html(title, body), encoding="utf-8")
    print(f"  OK: {name}.html ({html_path.stat().st_size // 1024} KB)")

print("Done.")
