from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping
from zipfile import ZIP_DEFLATED, ZipFile

from lxml import etree


W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": W_NS}
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"

SIMPLE_FIELDS = ("姓名", "年龄", "性别", "在读年级", "测评日期", "绘画主题")


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    value = str(value).strip()
    return value if value else default


def normalize_payload(payload: Mapping[str, Any]) -> dict[str, str]:
    """Accept both Chinese field names and the English Coze variable names."""

    def pick(*names: str, default: str = "") -> str:
        for name in names:
            if name in payload and _text(payload[name]):
                return _text(payload[name])
        return default

    age = pick("年龄", "age")
    if age and not age.endswith("岁"):
        age = f"{age}岁"

    data = {
        "姓名": pick("姓名", "child_name"),
        "年龄": age,
        "性别": pick("性别", "gender"),
        "在读年级": pick("在读年级", "grade", default="—"),
        "测评日期": pick("测评日期", "test_date", default="—"),
        "绘画主题": pick("绘画主题", "drawing_theme", default="树木画"),
    }

    interpretation = pick("测评师解读", "interpretation")
    if not interpretation:
        sections = (
            ("画面观察", pick("画面观察", "observation")),
            ("专业结论", pick("专业结论", "conclusion")),
            ("家长关注重点", pick("家长关注重点", "parent_focus")),
            ("沟通建议", pick("沟通建议", "advice")),
        )
        interpretation = "\n\n".join(
            f"{label}：{content}" for label, content in sections if content
        )

    data["测评师解读"] = interpretation
    return data


def validate_payload(data: Mapping[str, str]) -> list[str]:
    required = {
        "姓名": "孩子姓名",
        "年龄": "孩子年龄",
        "性别": "孩子性别",
        "测评师解读": "测评师解读",
    }
    return [label for key, label in required.items() if not _text(data.get(key))]


def _paragraph_text(paragraph: etree._Element) -> str:
    return "".join(paragraph.xpath(".//w:t/text()", namespaces=NS))


def _replace_text_in_paragraph(paragraph: etree._Element, old: str, new: str) -> bool:
    text_nodes = paragraph.xpath(".//w:t", namespaces=NS)
    if not text_nodes:
        return False
    combined = "".join(node.text or "" for node in text_nodes)
    if old not in combined:
        return False
    replacement = combined.replace(old, new)
    text_nodes[0].text = replacement
    text_nodes[0].set(XML_SPACE, "preserve")
    for node in text_nodes[1:]:
        node.text = ""
    return True


def _make_content_paragraph(source: etree._Element, content: str) -> etree._Element:
    paragraph = etree.Element(f"{{{W_NS}}}p", nsmap=source.nsmap)
    ppr = source.find("w:pPr", namespaces=NS)
    if ppr is not None:
        paragraph.append(deepcopy(ppr))
        # Keep four generated sections together with the template's support
        # notice. The source placeholder is intentionally spacious; using a
        # slightly tighter 1.2 line spacing avoids an orphan third page while
        # remaining comfortable to read in Word/WPS.
        spacing = paragraph.find("w:pPr/w:spacing", namespaces=NS)
        if spacing is not None:
            spacing.set(f"{{{W_NS}}}after", "60")
            spacing.set(f"{{{W_NS}}}line", "288")
            spacing.set(f"{{{W_NS}}}lineRule", "auto")

    run = etree.SubElement(paragraph, f"{{{W_NS}}}r")
    source_run = source.find("w:r", namespaces=NS)
    if source_run is not None:
        rpr = source_run.find("w:rPr", namespaces=NS)
        if rpr is not None:
            run.append(deepcopy(rpr))

    text = etree.SubElement(run, f"{{{W_NS}}}t")
    text.set(XML_SPACE, "preserve")
    text.text = content
    return paragraph


def _replace_interpretation(root: etree._Element, content: str) -> bool:
    placeholder = "{{测评师解读}}"
    for paragraph in root.xpath(".//w:p", namespaces=NS):
        if placeholder not in _paragraph_text(paragraph):
            continue

        chunks = [part.strip() for part in content.replace("\r\n", "\n").split("\n\n") if part.strip()]
        if not chunks:
            chunks = ["—"]

        parent = paragraph.getparent()
        index = parent.index(paragraph)
        for offset, chunk in enumerate(chunks):
            # Single newlines stay inside one paragraph as spaces.
            chunk = " ".join(line.strip() for line in chunk.split("\n") if line.strip())
            parent.insert(index + offset, _make_content_paragraph(paragraph, chunk))
        parent.remove(paragraph)
        return True
    return False


def generate_report(template_path: Path, payload: Mapping[str, Any], output_path: Path) -> dict[str, str]:
    data = normalize_payload(payload)
    missing = validate_payload(data)
    if missing:
        raise ValueError("缺少必填字段：" + "、".join(missing))

    with ZipFile(template_path, "r") as source_zip:
        archive = {name: source_zip.read(name) for name in source_zip.namelist()}

    document_name = "word/document.xml"
    parser = etree.XMLParser(remove_blank_text=False)
    root = etree.fromstring(archive[document_name], parser)

    for field in SIMPLE_FIELDS:
        placeholder = "{{" + field + "}}"
        value = _text(data.get(field), "—")
        replaced = False
        for paragraph in root.xpath(".//w:p", namespaces=NS):
            replaced = _replace_text_in_paragraph(paragraph, placeholder, value) or replaced
        if not replaced:
            raise ValueError(f"模板中找不到占位符：{placeholder}")

    if not _replace_interpretation(root, data["测评师解读"]):
        raise ValueError("模板中找不到占位符：{{测评师解读}}")

    archive[document_name] = etree.tostring(
        root,
        xml_declaration=True,
        encoding="UTF-8",
        standalone=True,
    )

    # The original template uses PingFang SC, which is not available on most
    # Linux/Render hosts. Match the original Skill's behavior and use an
    # openly available CJK font name so Word/WPS can substitute consistently.
    for name, content in list(archive.items()):
        if name.endswith(".xml"):
            archive[name] = content.replace(b"PingFang SC", b"Noto Sans CJK SC")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output_path, "w", compression=ZIP_DEFLATED) as target_zip:
        for name, content in archive.items():
            target_zip.writestr(name, content)

    return data
