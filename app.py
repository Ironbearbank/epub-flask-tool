import os
import tempfile
import time
import traceback
import uuid

from flask import Flask, render_template, request, send_file, flash, redirect, url_for, abort

import epub_core

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.config["MAX_CONTENT_LENGTH"] = 512 * 1024 * 1024  # 512MB (写真集epubは数百MBになりうるため)

# 生成したepubを一時的に保持し、プレビュー確認後にダウンロードできるようにする。
# プロセスメモリ上に保持するだけの簡易実装(内部ツール規模を想定)。
_PENDING_DOWNLOADS = {}
_DOWNLOAD_TTL_SECONDS = 60 * 60  # 1時間で破棄


def _cleanup_pending_downloads():
    now = time.time()
    expired = [t for t, v in _PENDING_DOWNLOADS.items() if now - v["created"] > _DOWNLOAD_TTL_SECONDS]
    for t in expired:
        entry = _PENDING_DOWNLOADS.pop(t, None)
        if entry:
            try:
                os.remove(entry["path"])
            except OSError:
                pass


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/check", methods=["GET", "POST"])
def check():
    if request.method == "GET":
        # 結果画面でリロードするとフォーム再送信ではなくGETになり、
        # POST専用ルートのままだと405エラーになるため、トップに戻す。
        flash("再度チェックする場合はファイルを選択してください。")
        return redirect(url_for("index"))

    file = request.files.get("epub_file")
    if not file or file.filename == "":
        flash("epubファイルを選択してください。")
        return redirect(url_for("index"))
    if not file.filename.lower().endswith(".epub"):
        flash("拡張子が.epubのファイルを選択してください。")
        return redirect(url_for("index"))

    with tempfile.TemporaryDirectory() as tmp:
        epub_path = os.path.join(tmp, file.filename)
        file.save(epub_path)
        try:
            result = epub_core.run_qa_check(epub_path)
        except Exception as e:
            traceback.print_exc()
            flash(f"チェック中にエラーが発生しました: {e}")
            return redirect(url_for("index"))

    return render_template("check_result.html", filename=file.filename, result=result)


@app.route("/build", methods=["GET", "POST"])
def build():
    if request.method == "GET":
        flash("再度生成する場合は画像を選択してください。")
        return redirect(url_for("index"))

    _cleanup_pending_downloads()

    title = request.form.get("title", "").strip()
    direction = request.form.get("direction", "rtl")
    first_side = request.form.get("first_side", "left")

    if not title:
        flash("タイトルを入力してください。")
        return redirect(url_for("index"))

    page_files = request.files.getlist("page_files")
    page_files = [f for f in page_files if f and f.filename]
    if not page_files:
        flash("本文ページ画像を選択してください(複数選択可)。")
        return redirect(url_for("index"))

    cover_file = request.files.get("cover_file")
    has_cover = cover_file and cover_file.filename

    with tempfile.TemporaryDirectory() as tmp:
        page_paths = []
        for i, f in enumerate(page_files):
            ext = os.path.splitext(f.filename)[1] or ".jpg"
            p = os.path.join(tmp, f"page_{i:04d}{ext}")
            f.save(p)
            page_paths.append(p)

        cover_path = None
        if has_cover:
            ext = os.path.splitext(cover_file.filename)[1] or ".jpg"
            cover_path = os.path.join(tmp, f"cover{ext}")
            cover_file.save(cover_path)

        # ダウンロード用ファイルは、このリクエスト用の一時ディレクトリの外
        # (プロセス全体で共有する場所)に保存し、TemporaryDirectory終了後も残す。
        safe_title = "".join(c for c in title if c.isalnum() or c in " _-") or "output"
        download_name = f"{safe_title}.epub"
        token = uuid.uuid4().hex
        persist_dir = tempfile.mkdtemp(prefix="epub_build_result_")
        output_path = os.path.join(persist_dir, "output.epub")

        try:
            result = epub_core.build_epub_from_images(
                page_paths, cover_path, title, output_path,
                direction=direction, first_side=first_side,
            )
        except Exception as e:
            traceback.print_exc()
            flash(f"生成中にエラーが発生しました: {e}")
            return redirect(url_for("index"))

    _PENDING_DOWNLOADS[token] = {
        "path": output_path,
        "filename": download_name,
        "created": time.time(),
    }

    return render_template(
        "build_result.html",
        title=title,
        result=result,
        download_token=token,
    )


@app.route("/rebuild", methods=["GET", "POST"])
def rebuild():
    if request.method == "GET":
        flash("再度実行する場合はepubファイルを選択してください。")
        return redirect(url_for("index"))

    _cleanup_pending_downloads()

    file = request.files.get("source_epub_file")
    if not file or file.filename == "":
        flash("再生成元のepubファイルを選択してください。")
        return redirect(url_for("index"))
    if not file.filename.lower().endswith(".epub"):
        flash("拡張子が.epubのファイルを選択してください。")
        return redirect(url_for("index"))

    title_override = request.form.get("title", "").strip() or None
    direction = request.form.get("direction", "rtl")
    first_side = request.form.get("first_side", "left")

    with tempfile.TemporaryDirectory() as tmp:
        epub_path = os.path.join(tmp, file.filename)
        file.save(epub_path)

        safe_stem = "".join(c for c in os.path.splitext(file.filename)[0] if c.isalnum() or c in " _-") or "output"
        download_name = f"{safe_stem}_rebuilt.epub"
        token = uuid.uuid4().hex
        persist_dir = tempfile.mkdtemp(prefix="epub_build_result_")
        output_path = os.path.join(persist_dir, "output.epub")

        try:
            result = epub_core.rebuild_epub_from_existing(
                epub_path, output_path, title_override=title_override,
                direction=direction, first_side=first_side,
            )
        except Exception as e:
            traceback.print_exc()
            flash(f"再生成中にエラーが発生しました: {e}")
            return redirect(url_for("index"))

    _PENDING_DOWNLOADS[token] = {
        "path": output_path,
        "filename": download_name,
        "created": time.time(),
    }

    return render_template(
        "build_result.html",
        title=title_override or file.filename,
        result=result,
        download_token=token,
    )


@app.route("/download/<token>")
def download(token):
    entry = _PENDING_DOWNLOADS.get(token)
    if not entry or not os.path.exists(entry["path"]):
        abort(404)
    return send_file(
        entry["path"],
        mimetype="application/epub+zip",
        as_attachment=True,
        download_name=entry["filename"],
    )


if __name__ == "__main__":
    app.run(debug=True, port=5050)
