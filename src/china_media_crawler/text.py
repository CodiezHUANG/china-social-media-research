"""Plain-text conversion for platform HTML fragments."""
from html.parser import HTMLParser


class PlainText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "img":
            alt = dict(attrs).get("alt")
            if alt:
                self.parts.append(alt)


def plain_text(value: str) -> str:
    parser = PlainText()
    parser.feed(value)
    parser.close()
    return "".join(parser.parts).strip()
