from __future__ import annotations

from copy import deepcopy
from io import BytesIO
from pathlib import Path
from typing import Any, Mapping
from zipfile import ZIP_DEFLATED, ZipFile

from lxml import etree
from PIL import Image, ImageOps


W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
WP_NS = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
PIC_NS = "http://schemas.openxmlformats.org/drawingml/2006/picture"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
NS = {"w": W_NS, "wp": WP_NS, "a": A_NS, "pic": PIC_NS, "r": R_NS}
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

    interpretation = pick(
        "测评师解读",
        "interpretation",
        "expert_conclusion",
    )
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


def _normalize_image(image_path: Path) -> tuple[bytes, int, int]:
    with Image.open(image_path) as source:
        source = ImageOps.exif_transpose(source)
        if source.mode in ("RGBA", "LA"):
            background = Image.new("RGB", source.size, "white")
            alpha = source.getchannel("A")
            background.paste(source.convert("RGB"), mask=alpha)
            source = background
        elif source.mode != "RGB":
            source = source.convert("RGB")
        pixel_width, pixel_height = source.size
        buffer = BytesIO()
        source.save(buffer, format="JPEG", quality=94, optimize=True)
        return buffer.getvalue(), pixel_width, pixel_height


def _next_relationship_id(relationships: etree._Element) -> str:
    used = set()
    for relationship in relationships:
        value = relationship.get("Id", "")
        if value.startswith("rId") and value[3:].isdigit():
            used.add(int(value[3:]))
    candidate = 1
    while candidate in used:
        candidate += 1
    return f"rId{candidate}"


def _page_break_paragraph() -> etree._Element:
    paragraph = etree.Element(f"{{{W_NS}}}p")
    run = etree.SubElement(paragraph, f"{{{W_NS}}}r")
    etree.SubElement(run, f"{{{W_NS}}}br").set(f"{{{W_NS}}}type", "page")
    return paragraph


def _image_paragraph(
    relationship_id: str,
    width_emu: int,
    height_emu: int,
    doc_pr_id: int,
) -> etree._Element:
    paragraph = etree.Element(f"{{{W_NS}}}p")
    ppr = etree.SubElement(paragraph, f"{{{W_NS}}}pPr")
    etree.SubElement(ppr, f"{{{W_NS}}}jc").set(f"{{{W_NS}}}val", "center")
    etree.SubElement(ppr, f"{{{W_NS}}}spacing").set(f"{{{W_NS}}}after", "100")

    run = etree.SubElement(paragraph, f"{{{W_NS}}}r")
    drawing = etree.SubElement(run, f"{{{W_NS}}}drawing")
    inline = etree.SubElement(
        drawing,
        f"{{{WP_NS}}}inline",
        nsmap={"wp": WP_NS, "a": A_NS, "pic": PIC_NS, "r": R_NS},
    )
    for attribute in ("distT", "distB", "distL", "distR"):
        inline.set(attribute, "0")
    extent = etree.SubElement(inline, f"{{{WP_NS}}}extent")
    extent.set("cx", str(width_emu))
    extent.set("cy", str(height_emu))
    effect_extent = etree.SubElement(inline, f"{{{WP_NS}}}effectExtent")
    for side in ("l", "t", "r", "b"):
        effect_extent.set(side, "0")
    doc_pr = etree.SubElement(inline, f"{{{WP_NS}}}docPr")
    doc_pr.set("id", str(doc_pr_id))
    doc_pr.set("name", "原始画作")
    doc_pr.set("descr", "孩子提交的原始绘画作品")
    frame_pr = etree.SubElement(inline, f"{{{WP_NS}}}cNvGraphicFramePr")
    etree.SubElement(frame_pr, f"{{{A_NS}}}graphicFrameLocks").set("noChangeAspect", "1")

    graphic = etree.SubElement(inline, f"{{{A_NS}}}graphic")
    graphic_data = etree.SubElement(graphic, f"{{{A_NS}}}graphicData")
    graphic_data.set("uri", PIC_NS)
    picture = etree.SubElement(graphic_data, f"{{{PIC_NS}}}pic")
    nv_pic_pr = etree.SubElement(picture, f"{{{PIC_NS}}}nvPicPr")
    c_nv_pr = etree.SubElement(nv_pic_pr, f"{{{PIC_NS}}}cNvPr")
    c_nv_pr.set("id", "0")
    c_nv_pr.set("name", "original-drawing.jpg")
    etree.SubElement(nv_pic_pr, f"{{{PIC_NS}}}cNvPicPr")

    blip_fill = etree.SubElement(picture, f"{{{PIC_NS}}}blipFill")
    etree.SubElement(blip_fill, f"{{{A_NS}}}blip").set(f"{{{R_NS}}}embed", relationship_id)
    stretch = etree.SubElement(blip_fill, f"{{{A_NS}}}stretch")
    etree.SubElement(stretch, f"{{{A_NS}}}fillRect")

    shape_pr = etree.SubElement(picture, f"{{{PIC_NS}}}spPr")
    transform = etree.SubElement(shape_pr, f"{{{A_NS}}}xfrm")
    offset = etree.SubElement(transform, f"{{{A_NS}}}off")
    offset.set("x", "0")
    offset.set("y", "0")
    dimensions = etree.SubElement(transform, f"{{{A_NS}}}ext")
    dimensions.set("cx", str(width_emu))
    dimensions.set("cy", str(height_emu))
    geometry = etree.SubElement(shape_pr, f"{{{A_NS}}}prstGeom")
    geometry.set("prst", "rect")
    etree.SubElement(geometry, f"{{{A_NS}}}avLst")
    return paragraph


def _append_original_drawing(archive: dict[str, bytes], image_path: Path) -> None:
    image_bytes, pixel_width, pixel_height = _normalize_image(image_path)

    parser = etree.XMLParser(remove_blank_text=False)
    document_root = etree.fromstring(archive["word/document.xml"], parser)
    relationships_root = etree.fromstring(archive["word/_rels/document.xml.rels"], parser)
    content_types_root = etree.fromstring(archive["[Content_Types].xml"], parser)

    relationship_id = _next_relationship_id(relationships_root)
    relationship = etree.SubElement(relationships_root, f"{{{PKG_REL_NS}}}Relationship")
    relationship.set("Id", relationship_id)
    relationship.set("Type", "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image")
    relationship.set("Target", "media/original-drawing.jpg")

    jpg_types = {
        element.get("Extension", "").lower()
        for element in content_types_root.findall(f"{{{CT_NS}}}Default")
    }
    if not {"jpg", "jpeg"}.intersection(jpg_types):
        default = etree.SubElement(content_types_root, f"{{{CT_NS}}}Default")
        default.set("Extension", "jpg")
        default.set("ContentType", "image/jpeg")

    body = document_root.find("w:body", namespaces=NS)
    if body is None:
        raise ValueError("模板正文结构无效")
    section_properties = body.find("w:sectPr", namespaces=NS)
    page_size = section_properties.find("w:pgSz", namespaces=NS) if section_properties is not None else None
    page_margins = section_properties.find("w:pgMar", namespaces=NS) if section_properties is not None else None

    page_width = int(page_size.get(f"{{{W_NS}}}w", "11906")) if page_size is not None else 11906
    page_height = int(page_size.get(f"{{{W_NS}}}h", "16838")) if page_size is not None else 16838
    left = int(page_margins.get(f"{{{W_NS}}}left", "1440")) if page_margins is not None else 1440
    right = int(page_margins.get(f"{{{W_NS}}}right", "1440")) if page_margins is not None else 1440
    top = int(page_margins.get(f"{{{W_NS}}}top", "1440")) if page_margins is not None else 1440
    bottom = int(page_margins.get(f"{{{W_NS}}}bottom", "1440")) if page_margins is not None else 1440

    # Use the printable area while preserving the source aspect ratio.
    max_width = min(page_width - left - right, 9216)
    max_height = min(page_height - top - bottom - 360, 11880)
    aspect_ratio = pixel_width / pixel_height
    width = max_width
    height = int(width / aspect_ratio)
    if height > max_height:
        height = max_height
        width = int(height * aspect_ratio)

    used_doc_pr_ids = [
        int(value)
        for value in document_root.xpath(".//wp:docPr/@id", namespaces=NS)
        if str(value).isdigit()
    ]
    doc_pr_id = max(used_doc_pr_ids, default=0) + 1

    appendix = (
        _page_break_paragraph(),
        _image_paragraph(relationship_id, width * 635, height * 635, doc_pr_id),
    )
    insert_at = body.index(section_properties) if section_properties is not None else len(body)
    for offset, element in enumerate(appendix):
        body.insert(insert_at + offset, element)

    archive["word/document.xml"] = etree.tostring(
        document_root, xml_declaration=True, encoding="UTF-8", standalone=True
    )
    archive["word/_rels/document.xml.rels"] = etree.tostring(
        relationships_root, xml_declaration=True, encoding="UTF-8", standalone=True
    )
    archive["[Content_Types].xml"] = etree.tostring(
        content_types_root, xml_declaration=True, encoding="UTF-8", standalone=True
    )
    archive["word/media/original-drawing.jpg"] = image_bytes


def generate_report(
    template_path: Path,
    payload: Mapping[str, Any],
    output_path: Path,
    image_path: Path | None = None,
) -> dict[str, str]:
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
    if image_path is not None:
        _append_original_drawing(archive, image_path)

    with ZipFile(output_path, "w", compression=ZIP_DEFLATED) as target_zip:
        for name, content in archive.items():
            target_zip.writestr(name, content)

    return data
