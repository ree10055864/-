from __future__ import annotations

import os
import socket
import time
import uuid
from ipaddress import ip_address
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from flask import Flask, jsonify, request, send_from_directory
from PIL import Image, UnidentifiedImageError

from report_generator import generate_report


BASE_DIR = Path(__file__).resolve().parent
TEMPLATE_PATH = BASE_DIR / "assets" / "绘画心理观察报告_模板v5.docx"
OUTPUT_DIR = BASE_DIR / "generated"
OUTPUT_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024

MAX_IMAGE_BYTES = int(os.environ.get("REPORT_MAX_IMAGE_BYTES", str(15 * 1024 * 1024)))
IMAGE_TIMEOUT_SECONDS = int(os.environ.get("REPORT_IMAGE_TIMEOUT_SECONDS", "20"))


def _authorized() -> bool:
    expected = os.environ.get("REPORT_API_KEY", "").strip()
    if not expected:
        return True
    supplied = request.headers.get("X-API-Key", "").strip()
    return supplied == expected


def _cleanup_old_reports() -> None:
    ttl_hours = int(os.environ.get("REPORT_TTL_HOURS", "24"))
    cutoff = time.time() - ttl_hours * 3600
    for path in OUTPUT_DIR.glob("*.docx"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


def _validate_public_https_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ValueError("原画地址必须是有效的HTTPS链接")

    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443)
    except OSError as exc:
        raise ValueError("无法解析原画地址") from exc

    for address in addresses:
        ip = ip_address(address[4][0])
        if not ip.is_global:
            raise ValueError("原画地址不能指向内网或本机")


class _SafeRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        absolute_url = urljoin(req.full_url, newurl)
        _validate_public_https_url(absolute_url)
        return super().redirect_request(req, fp, code, msg, headers, absolute_url)


def _drawing_image_url(payload: dict) -> str:
    value = payload.get("drawing_image_url") or payload.get("drawing_image") or ""
    if isinstance(value, dict):
        value = value.get("url") or value.get("download_url") or ""
    return str(value).strip()


def _download_drawing_image(url: str, destination: Path) -> None:
    _validate_public_https_url(url)
    request_obj = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 DrawingReportGenerator/1.0",
            "Accept": "image/*,*/*;q=0.8",
        },
    )
    opener = build_opener(_SafeRedirectHandler())

    try:
        with opener.open(request_obj, timeout=IMAGE_TIMEOUT_SECONDS) as response:
            content_type = response.headers.get_content_type()
            if content_type != "application/octet-stream" and not content_type.startswith("image/"):
                raise ValueError(f"原画链接返回的不是图片（{content_type}）")

            total = 0
            with destination.open("wb") as output:
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_IMAGE_BYTES:
                        raise ValueError("原画文件过大")
                    output.write(chunk)
    except HTTPError as exc:
        raise ValueError(f"下载原画失败（HTTP {exc.code}），飞书链接可能已过期或无权限") from exc
    except URLError as exc:
        raise ValueError("下载原画失败，请检查飞书图片链接是否仍然有效") from exc

    try:
        with Image.open(destination) as image:
            image.verify()
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("原画链接返回的文件不是可识别的图片") from exc


@app.get("/")
def index():
    return jsonify(
        service="drawing-report-generator",
        status="ok",
        endpoint="POST /generate-report",
    )


@app.get("/health")
def health():
    return jsonify(status="ok", template_exists=TEMPLATE_PATH.exists())


@app.post("/generate-report")
def create_report():
    if not _authorized():
        return jsonify(success=False, error="unauthorized"), 401

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify(success=False, error="请求体必须是JSON对象"), 400

    _cleanup_old_reports()
    token = uuid.uuid4().hex
    filename = f"drawing-report-{token}.docx"
    output_path = OUTPUT_DIR / filename
    image_url = _drawing_image_url(payload)

    try:
        with TemporaryDirectory(prefix="drawing-report-") as temp_dir:
            image_path = None
            if image_url:
                image_path = Path(temp_dir) / "original-drawing"
                _download_drawing_image(image_url, image_path)

            normalized = generate_report(
                TEMPLATE_PATH,
                payload,
                output_path,
                image_path=image_path,
            )
    except ValueError as exc:
        return jsonify(success=False, error=str(exc)), 400
    except Exception as exc:
        app.logger.exception("report generation failed")
        return jsonify(success=False, error="报告生成失败", detail=str(exc)), 500

    report_url = request.url_root.rstrip("/") + f"/reports/{filename}"
    return jsonify(
        success=True,
        report_url=report_url,
        file_name=filename,
        child_name=normalized["姓名"],
        drawing_attached=bool(image_url),
    )


@app.get("/reports/<path:filename>")
def download_report(filename: str):
    return send_from_directory(
        OUTPUT_DIR,
        filename,
        as_attachment=True,
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
