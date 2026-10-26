import os
import tempfile
import traceback

from flask import Flask, render_template, request, send_file, flash, redirect, url_for

import epub_core

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.config["MAX_CONTENT_LENGTH"] = 512 * 1024 * 1024  # 512MB (写真集epubは数百MBになりうるため)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/check", methods=["POST"])
def check():
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


@app.route("/build", methods=["POST"])
def build():
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

        output_path = os.path.join(tmp, "output.epub")
        try:
            epub_core.build_epub_from_images(
                page_paths, cover_path, title, output_path,
                direction=direction, first_side=first_side,
            )
        except Exception as e:
            traceback.print_exc()
            flash(f"生成中にエラーが発生しました: {e}")
            return redirect(url_for("index"))

        safe_title = "".join(c for c in title if c.isalnum() or c in " _-") or "output"
        download_name = f"{safe_title}.epub"
        # send_file needs the file to still exist after the TemporaryDirectory
        # context closes, so read into memory for small/medium books; for very
        # large books this copies once, which is acceptable for this tool's scale.
        with open(output_path, "rb") as f:
            data = f.read()

    import io
    return send_file(
        io.BytesIO(data),
        mimetype="application/epub+zip",
        as_attachment=True,
        download_name=download_name,
    )


if __name__ == "__main__":
    app.run(debug=True, port=5050)
