from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from report_generator import generate_report


BASE_DIR = Path(__file__).resolve().parent
TEMPLATE_PATH = BASE_DIR / "assets" / "绘画心理观察报告_模板v5.docx"
OUTPUT_DIR = BASE_DIR / "generated"
OUTPUT_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024


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

    try:
        normalized = generate_report(TEMPLATE_PATH, payload, output_path)
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

