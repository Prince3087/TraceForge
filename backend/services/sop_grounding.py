"""SOP grounding engine.

Parses the SOP markdown into (section, page) blocks and provides *deterministic*
verification of any citation the LLM produces. The LLM is never trusted: a citation
is valid only if its quote is a verbatim substring (whitespace/markdown-insensitive)
of the claimed SOP section, on the claimed page.

It also extracts the Section 4.2.2 execution options verbatim so every remediation
action offered to the operator traces word-for-word to the SOP.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

REQUIRED_SECTION = "4.2"

_PAGE_RE = re.compile(r"<!--\s*PAGE BREAK:\s*Page\s+(\d+)\s+of\s+(\d+)\s*-->", re.I)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(\d+(?:\.\d+)*)\.?\s+(.+?)\s*$")
_SECTION_NUM_RE = re.compile(r"\d+(?:\.\d+)*")


class SopError(RuntimeError):
    """Raised when the SOP source cannot be loaded or parsed."""


def normalize(text: str) -> str:
    """Strip markdown decoration and collapse whitespace. Case and punctuation are kept."""
    cleaned_lines = []
    for line in text.splitlines():
        line = re.sub(r"^\s*>+\s?", "", line)  # blockquote marker
        line = re.sub(r"^\s*(?:[-*]|\d+\.)\s+", "", line)  # list marker
        cleaned_lines.append(line)
    out = " ".join(cleaned_lines)
    out = out.replace("**", "").replace("__", "").replace("`", "")
    return re.sub(r"\s+", " ", out).strip()


@dataclass(frozen=True)
class SopBlock:
    number: str
    title: str
    page: int
    text: str  # normalized


@dataclass(frozen=True)
class SopOption:
    option_id: str  # OPTION_A / OPTION_B
    label: str  # "Option A (Full Stop)"
    text: str  # verbatim SOP sentence(s), markdown stripped
    pause_min: int | None = None
    derate_pct: float | None = None
    derate_max_hours: float | None = None


@dataclass(frozen=True)
class CitationCheck:
    valid: bool
    reason: str
    resolved_page: int | None = None


@dataclass(frozen=True)
class SopDocument:
    document_id: str
    source_file: str
    total_pages: int
    blocks: tuple[SopBlock, ...]
    raw_text: str
    accepted_ids: frozenset[str] = field(default_factory=frozenset)

    # ----------------------------------------------------------------- lookups
    def section_blocks(self, number: str) -> list[SopBlock]:
        return [b for b in self.blocks if b.number == number or b.number.startswith(number + ".")]

    def page_of(self, number: str) -> int | None:
        blocks = [b for b in self.blocks if b.number == number]
        return blocks[0].page if blocks else None

    def section_text(self, number: str) -> str:
        return " ".join(b.text for b in self.section_blocks(number))

    def full_text(self) -> str:
        return " ".join(b.text for b in self.blocks)

    # ------------------------------------------------------------ verification
    def verify_citation(
        self, sop_document_id: str, section: str, page_number: int, verbatim_quote: str
    ) -> CitationCheck:
        claimed_id = re.sub(r"\.(pdf|md)$", "", sop_document_id.strip(), flags=re.I).upper()
        if claimed_id not in self.accepted_ids:
            return CitationCheck(False, f"Unknown SOP document id '{sop_document_id}'.")

        m = _SECTION_NUM_RE.search(section or "")
        if not m:
            return CitationCheck(False, f"Cannot parse section number from '{section}'.")
        claimed_section = m.group(0)
        if claimed_section != REQUIRED_SECTION and not claimed_section.startswith(
            REQUIRED_SECTION + "."
        ):
            return CitationCheck(
                False,
                f"Section {claimed_section} is not within SOP Section {REQUIRED_SECTION}, "
                "the only section authorised for remediation guidance.",
            )

        quote = normalize(verbatim_quote or "")
        if not quote:
            return CitationCheck(False, "Empty quote.")

        for block in self.section_blocks(REQUIRED_SECTION):
            if quote in block.text:
                if block.page != page_number:
                    return CitationCheck(
                        False,
                        f"Quote found in Section {REQUIRED_SECTION} but on page {block.page}, "
                        f"not page {page_number}.",
                        block.page,
                    )
                return CitationCheck(True, "Quote is verbatim in claimed section and page.", block.page)

        # Cross-section spillover (e.g. quote spans 4.2 -> 4.2.2 headings) is checked on the joined text.
        if quote in self.section_text(REQUIRED_SECTION):
            return CitationCheck(True, "Quote is verbatim in claimed section.", self.page_of(REQUIRED_SECTION))

        where = "elsewhere in the SOP" if quote in self.full_text() else "nowhere in the SOP"
        return CitationCheck(False, f"Quote is not verbatim in Section {REQUIRED_SECTION} (found {where}).")

    def text_is_grounded(self, text: str, section: str = REQUIRED_SECTION) -> bool:
        needle = normalize(text)
        return bool(needle) and needle in self.section_text(section)

    def directive_quote(self) -> str:
        """Verbatim §4.2.1 compliance directive sentence(s), extracted from the SOP text."""
        text = " ".join(b.text for b in self.blocks if b.number == REQUIRED_SECTION)
        m = re.search(r"\(§4\.2\.1\):\s*(.+)$", text)
        if not m:
            raise SopError("SOP Section 4.2 has no §4.2.1 compliance directive.")
        return m.group(1).strip()

    # --------------------------------------------------------------- options
    def execution_options(self) -> dict[str, SopOption]:
        """Extract Option A / Option B verbatim from Section 4.2.2."""
        options: dict[str, SopOption] = {}
        for line in self.raw_text.splitlines():
            stripped = re.sub(r"^\s*(?:[-*]|\d+\.)\s+", "", line).replace("**", "").strip()
            m = re.match(r"^(Option ([AB]) \(([^)]+)\)):\s*(.+)$", stripped)
            if not m:
                continue
            label, letter, _, body = m.group(1), m.group(2), m.group(3), m.group(4)
            text = f"{label}: {body}".strip()
            pause = re.search(r"(\d+)-minute", body)
            derate = re.search(r"by (\d+(?:\.\d+)?)%", body)
            max_h = re.search(r"maximum of (\d+(?:\.\d+)?) hours?", body)
            options[f"OPTION_{letter}"] = SopOption(
                option_id=f"OPTION_{letter}",
                label=label,
                text=text,
                pause_min=int(pause.group(1)) if pause else None,
                derate_pct=float(derate.group(1)) if derate else None,
                derate_max_hours=float(max_h.group(1)) if max_h else None,
            )
        missing = {"OPTION_A", "OPTION_B"} - options.keys()
        if missing:
            raise SopError(f"SOP Section 4.2.2 is missing execution options: {sorted(missing)}")
        a, b = options["OPTION_A"], options["OPTION_B"]
        if a.pause_min is None or b.derate_pct is None or b.derate_max_hours is None:
            raise SopError("Could not extract pause/derate parameters from SOP Section 4.2.2.")
        return options


def _parse(md: str, source_file: str) -> SopDocument:
    # Front matter -------------------------------------------------------------------------
    document_id = None
    body = md
    if md.startswith("---"):
        end = md.find("\n---", 3)
        if end != -1:
            front = md[3:end]
            body = md[end + 4 :]
            m = re.search(r'document_id:\s*"?([^"\n]+)"?', front)
            if m:
                document_id = m.group(1).strip()
    if not document_id:
        raise SopError("SOP front matter has no document_id.")

    # Blocks (page tracked through PAGE BREAK markers) -------------------------------------
    page = 1
    total_pages = 1
    blocks: list[SopBlock] = []
    cur_num = cur_title = None
    cur_page = 1
    cur_lines: list[str] = []

    def flush() -> None:
        nonlocal cur_num, cur_title, cur_lines
        if cur_num is not None:
            blocks.append(SopBlock(cur_num, cur_title or "", cur_page, normalize("\n".join(cur_lines))))
        cur_num, cur_title, cur_lines = None, None, []

    for line in body.splitlines():
        pm = _PAGE_RE.search(line)
        if pm:
            page = int(pm.group(1))
            total_pages = max(total_pages, int(pm.group(2)))
            continue
        hm = _HEADING_RE.match(line)
        if hm:
            flush()
            cur_num, cur_title, cur_page = hm.group(2), hm.group(3), page
            continue
        if line.strip() == "---":
            continue
        if cur_num is not None:
            cur_lines.append(line)
    flush()

    if not any(b.number == REQUIRED_SECTION for b in blocks):
        raise SopError(f"SOP has no Section {REQUIRED_SECTION}.")

    stem = Path(source_file).stem.upper()
    accepted = {document_id.upper(), stem}
    return SopDocument(
        document_id=document_id,
        source_file=source_file,
        total_pages=total_pages,
        blocks=tuple(blocks),
        raw_text=body,
        accepted_ids=frozenset(accepted),
    )


@lru_cache(maxsize=4)
def load_sop(path: str) -> SopDocument:
    p = Path(path)
    if not p.is_file():
        raise SopError(f"SOP source not found: {p}")
    return _parse(p.read_text(encoding="utf-8"), p.name)
