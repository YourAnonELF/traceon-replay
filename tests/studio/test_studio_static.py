"""Static policy checks on the packaged UI: no inline code, no external network, safe DOM APIs."""

from __future__ import annotations

import re
import shutil
import subprocess
from html.parser import HTMLParser
from importlib import resources
from pathlib import Path

import pytest

STATIC = Path(str(resources.files("traceon.studio").joinpath("static")))
JS_FILES = sorted(STATIC.glob("*.js"))
BRAND = Path(__file__).resolve().parents[2] / "brand"


class _Audit(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.scripts: list[dict[str, str | None]] = []
        self.inline_script_bodies: list[str] = []
        self.event_handlers: list[str] = []
        self.style_attrs: list[str] = []
        self.style_tags = 0
        self.external: list[str] = []
        self._in_script = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        d = dict(attrs)
        for name, value in attrs:
            if name.startswith("on"):
                self.event_handlers.append(f"{tag}[{name}]")
            if name == "style":
                self.style_attrs.append(tag)
            if name in ("src", "href") and value and re.match(r"^(https?:)?//", value):
                self.external.append(value)
        if tag == "script":
            self.scripts.append(d)
            self._in_script = True
        if tag == "style":
            self.style_tags += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._in_script = False

    def handle_data(self, data: str) -> None:
        if self._in_script and data.strip():
            self.inline_script_bodies.append(data)


def test_index_has_no_inline_script_or_style() -> None:
    audit = _Audit()
    audit.feed((STATIC / "index.html").read_text(encoding="utf-8"))
    assert audit.scripts, "expected external scripts"
    assert all(s.get("src") for s in audit.scripts)
    assert audit.inline_script_bodies == []
    assert audit.event_handlers == []
    assert audit.style_attrs == []
    assert audit.style_tags == 0
    assert audit.external == []


@pytest.mark.parametrize("path", sorted(STATIC.iterdir()), ids=lambda p: p.name)
def test_no_external_urls(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    urls = re.findall(r"https?://[^\s\"')]+", text)
    assert [u for u in urls if u != "http://www.w3.org/2000/svg"] == []
    assert "@import" not in text
    assert "fonts.googleapis" not in text


@pytest.mark.parametrize("path", JS_FILES, ids=lambda p: p.name)
def test_js_avoids_html_injection_sinks(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    for sink in (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "eval(",
        "new Function",
        'setAttribute("style"',
        "javascript:",
    ):
        assert sink not in text, f"{path.name} uses {sink}"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize("path", JS_FILES, ids=lambda p: p.name)
def test_js_parses(path: Path) -> None:
    node = shutil.which("node")
    assert node is not None
    flag = [] if path.name == "theme.js" else ["--input-type=module"]
    source = path.read_text(encoding="utf-8")
    if flag:
        # Parse as an ES module without resolving imports.
        result = subprocess.run(
            [node, "--check", *flag],
            input=source,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
    else:
        result = subprocess.run(
            [node, "--check", str(path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
    assert result.returncode == 0, result.stderr


def test_css_has_no_decoration_regressions() -> None:
    css = (STATIC / "styles.css").read_text(encoding="utf-8")
    assert "gradient" not in css
    assert "box-shadow" not in css
    assert "backdrop-filter" not in css
    for radius in re.findall(r"border-radius:\s*([^;]+);", css):
        if radius.strip() == "var(--radius)":
            continue
        assert re.fullmatch(r"[0-3](px)?", radius.strip()), radius


def test_favicon_matches_brand() -> None:
    assert (STATIC / "favicon.svg").read_bytes() == (BRAND / "favicon.svg").read_bytes()


@pytest.mark.parametrize(
    "name", ["mark.svg", "logo.svg", "logo-dark.svg", "favicon.svg", "social-preview.svg"]
)
def test_brand_svgs_are_self_contained(name: str) -> None:
    text = (BRAND / name).read_text(encoding="utf-8")
    assert text.startswith('<svg xmlns="http://www.w3.org/2000/svg"')
    assert "<script" not in text and "<style" not in text and "href=" not in text
    assert "<title>" in text
