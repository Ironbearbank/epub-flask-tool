"""
marche向けフォトブック(epub)の、チェック/生成の中核ロジック。

check_epub_images.py(QAチェック)と build_epub_from_images.py(画像フォルダ
からの新規生成)の2本のCLIツールを元に、Webアプリから呼び出せる形に
まとめたもの。構造の詳細な背景(なぜSVG埋め込み形式を使うか等)は
元のCLIツールのdocstringおよびメモリ project_epub_indesign_vs_svg_compat を参照。
"""

import base64
import io
import os
import re
import shutil
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter

from PIL import Image

PREVIEW_PAGE_HEIGHT = 360  # プレビューサムネイル1ページあたりの高さ(px)

OPF_NS = "{http://www.idpf.org/2007/opf}"

PAGE_TMPL = '''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="ja">
    <head>
        <meta charset="UTF-8"/>
        <title>{title}</title>
        <link rel="stylesheet" href="../style/fixed-layout-jp.css" type="text/css" />
        <meta name="viewport" content="width={w}, height={h}" />
    </head>
    <body{cover_attr}>
        <div class="main">
            <svg xmlns="http://www.w3.org/2000/svg" version="1.1" xmlns:xlink="http://www.w3.org/1999/xlink"
                 width="100%" height="100%" viewBox="0 0 {w} {h}">
                <image width="{w}" height="{h}" xlink:href="../image/{img}" />
            </svg>
        </div>
    </body>
</html>
'''

CSS = '''@charset "UTF-8";
html,body {
  margin: 0;
  padding: 0;
  font-size: 0;
}
svg {
  margin: 0;
  padding: 0;
}
'''


# ---------- QAチェック (check_epub_images.py 相当) ----------

def find_opf_path(root_dir):
    container_path = os.path.join(root_dir, "META-INF", "container.xml")
    tree = ET.parse(container_path)
    ns = "{urn:oasis:names:tc:opendocument:xmlns:container}"
    rootfile = tree.find(f".//{ns}rootfile")
    return os.path.join(root_dir, rootfile.attrib["full-path"])


def parse_opf(opf_path):
    tree = ET.parse(opf_path)
    root = tree.getroot()

    manifest_items = root.findall(f".//{OPF_NS}manifest/{OPF_NS}item")
    ids = [item.attrib.get("id") for item in manifest_items]
    id_counts = Counter(ids)
    duplicate_ids = sorted({i for i, c in id_counts.items() if c > 1})

    manifest = {}
    for item in manifest_items:
        manifest[item.attrib.get("id")] = {
            "href": item.attrib.get("href"),
            "media-type": item.attrib.get("media-type", ""),
        }

    spine_items = root.findall(f".//{OPF_NS}spine/{OPF_NS}itemref")
    spine_hrefs = []
    spine_properties = []
    for itemref in spine_items:
        idref = itemref.attrib.get("idref")
        item = manifest.get(idref)
        if item:
            spine_hrefs.append(item["href"])
            spine_properties.append(itemref.attrib.get("properties", ""))

    generator = ""
    for meta in root.findall(f".//{OPF_NS}metadata/{OPF_NS}meta"):
        if meta.attrib.get("name") == "generator":
            generator = meta.attrib.get("content", "")
            break

    return manifest, spine_hrefs, duplicate_ids, spine_properties, generator


def find_viewport_size(opf_dir, spine_hrefs):
    pattern = re.compile(r'width\s*=\s*(\d+)\s*,\s*height\s*=\s*(\d+)')
    for href in spine_hrefs:
        path = os.path.join(opf_dir, href)
        if not os.path.exists(path) or not href.endswith((".xhtml", ".html")):
            continue
        try:
            content = open(path, encoding="utf-8").read()
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        m = pattern.search(content)
        if m:
            return int(m.group(1)), int(m.group(2))
    return None


def collect_images(opf_dir, spine_hrefs):
    src_pattern = re.compile(r'<(?:img|image)\b[^>]*?(?:src|xlink:href)="([^"]+)"')
    pages = []
    for href in spine_hrefs:
        path = os.path.join(opf_dir, href)
        if not os.path.exists(path):
            continue
        try:
            content = open(path, encoding="utf-8").read()
        except UnicodeDecodeError:
            continue
        for src in src_pattern.findall(content):
            img_path = os.path.normpath(os.path.join(os.path.dirname(path), src))
            pages.append((os.path.basename(href), img_path))
    return pages


def is_ascii_safe(name):
    return all(ord(c) < 128 for c in name) and " " not in name


def collect_representative_images(opf_dir, spine_hrefs):
    """spine順に、各ページを代表する画像パスを1枚ずつ返す(プレビュー用)。

    1ページに複数画像(オーバーレイ等)がある場合は、ファイルサイズが
    最大のものを代表画像とみなす(小さな装飾オーバーレイを避けるため)。
    """
    src_pattern = re.compile(r'<(?:img|image)\b[^>]*?(?:src|xlink:href)="([^"]+)"')
    reps = []
    for href in spine_hrefs:
        path = os.path.join(opf_dir, href)
        if not os.path.exists(path):
            reps.append(None)
            continue
        try:
            content = open(path, encoding="utf-8").read()
        except UnicodeDecodeError:
            reps.append(None)
            continue
        candidates = []
        for src in src_pattern.findall(content):
            img_path = os.path.normpath(os.path.join(os.path.dirname(path), src))
            if os.path.exists(img_path):
                candidates.append(img_path)
        if not candidates:
            reps.append(None)
            continue
        candidates.sort(key=lambda p: os.path.getsize(p), reverse=True)
        reps.append(candidates[0])
    return reps


def group_into_spreads(spine_properties):
    """spineのproperties列から、見開き(1〜2ページ)のグルーピングを推定する。

    ルール(今回のFRUiTS対応で実機確認した挙動をもとにしたヒューリスティック):
    - page-spread-center(表紙など) は常に単独。
    - 表紙(center)の直後2ページは、left/rightプロパティに関わらず
      常にそれぞれ単独表示になる(実機確認済み。bubka等の「表紙の次は
      1ページだけ単独」パターンに近いが、FRUiTSでは2ページ分だった)。
    - それ以降は、page-spread-left の直後に page-spread-right が続く
      場合のみペアになる。それ以外(連続するleft/連続するright等)は
      単独ページとして扱う。

    あくまで簡易プレビュー用の近似であり、実際のmarche_viewer表示を
    100%保証するものではない(実機確認は別途必須)。
    """
    spreads = []
    i = 0
    n = len(spine_properties)
    just_after_center = 0  # 表紙直後、残り何ページを強制的に単独扱いするか
    while i < n:
        prop = spine_properties[i]
        if "page-spread-center" in prop:
            spreads.append([i])
            i += 1
            just_after_center = 2
            continue
        if just_after_center > 0:
            spreads.append([i])
            i += 1
            just_after_center -= 1
            continue
        if "page-spread-left" in prop and i + 1 < n and "page-spread-right" in spine_properties[i + 1]:
            spreads.append([i, i + 1])
            i += 2
            continue
        spreads.append([i])
        i += 1
    return spreads


def _load_small_thumb(path, target_h):
    """大きな画像でもメモリを使いすぎないよう、低解像度のまま縮小して読み込む。

    Image.draft()(JPEGのみ対応)でデコード時点から縮小することに加え、
    Image.Image.thumbnail()を使って段階的に縮小することで、フル解像度
    (写真集だと2000x3000px超)をそのままメモリ展開するのを避ける
    (Render無料プランの512MB上限対策)。
    """
    with Image.open(path) as im:
        if im.format == "JPEG":
            im.draft("RGB", (target_h, target_h))
        im = im.convert("RGB")
        im.thumbnail((target_h * 10, target_h), Image.LANCZOS)
        # thumbnailはin-place操作だが、元ファイルハンドルから独立した
        # 新しいバッファとして確実にコピーしておく(withブロックを抜けた後も使うため)
        thumb = im.copy()
    return thumb


def build_preview_thumbnails(image_paths, spreads, max_spreads=20):
    """見開きごとのサムネイル(base64 JPEG)のリストを返す。

    image_paths: spine順の代表画像パスのリスト(Noneもあり得る)
    spreads: group_into_spreads() の戻り値

    max_spreads はデフォルトで少なめに制限している。QAチェックの目的は
    「見開き構成が壊れていないか」の目視確認であり、冒頭数見開きを見れば
    十分なことが多いため、写真集全体(数十〜百見開き)を毎回処理して
    メモリを使い切るよりも安全側に倒している。
    """
    import gc

    thumbs = []
    truncated = len(spreads) > max_spreads
    h = PREVIEW_PAGE_HEIGHT
    for n, spread in enumerate(spreads[:max_spreads]):
        resized = []
        for idx in spread:
            path = image_paths[idx] if idx < len(image_paths) else None
            if path and os.path.exists(path):
                try:
                    resized.append(_load_small_thumb(path, h))
                except Exception:
                    resized.append(None)
            else:
                resized.append(None)

        total_w = sum((im.width if im else int(h * 0.71)) for im in resized) or 1
        canvas = Image.new("RGB", (total_w, h), (40, 40, 40))
        x = 0
        for im in resized:
            if im is None:
                x += int(h * 0.71)
                continue
            canvas.paste(im, (x, 0))
            x += im.width

        buf = io.BytesIO()
        canvas.save(buf, format="JPEG", quality=80)
        thumbs.append(base64.b64encode(buf.getvalue()).decode("ascii"))

        del resized, canvas, buf
        if n % 4 == 3:
            gc.collect()

    return thumbs, truncated


def check_spread_structure(spine_properties, generator):
    warnings = []
    is_indesign = "indesign" in generator.lower()
    has_center = any("page-spread-center" in p for p in spine_properties)

    if is_indesign and has_center:
        warnings.append(
            "InDesign書き出しのepubに表紙(page-spread-center)が含まれています。"
            "この組み合わせはmarche_viewerで見開きペアリング崩れ(黒いページの"
            "挿入等)を起こした実績があるため、アップロード後に実機で全ページ"
            "の見開きを目視確認することを推奨します。再現する場合はSVG埋め込み"
            "形式での再構築(このツールの「epub生成」機能)を検討してください。"
        )

    lr_sequence = [p for p in spine_properties if "page-spread-left" in p or "page-spread-right" in p]
    sides = ["left" if "left" in p else "right" for p in lr_sequence]
    non_alternating_at = [i for i in range(1, len(sides)) if sides[i] == sides[i - 1]]
    if non_alternating_at:
        warnings.append(
            f"spine内でleft/rightが連続している箇所が{len(non_alternating_at)}件あります"
            "(見開きが正しく組まれていない、または単独ページが連続している可能性。"
            "実機確認を推奨)。"
        )

    return warnings


def run_qa_check(epub_path, tolerance=0.01):
    """QAチェックを実行し、結果をdictで返す(Webアプリ表示用)。"""
    tmp_dir = tempfile.mkdtemp(prefix="epub_check_")
    try:
        with zipfile.ZipFile(epub_path) as z:
            z.extractall(tmp_dir)

        opf_path = find_opf_path(tmp_dir)
        opf_dir = os.path.dirname(opf_path)
        manifest, spine_hrefs, duplicate_ids, spine_properties, generator = parse_opf(opf_path)
        viewport = find_viewport_size(opf_dir, spine_hrefs)
        pages = collect_images(opf_dir, spine_hrefs)

        if not pages:
            return {"fatal_error": "spineから画像を1件も検出できませんでした。EPUB構造を確認してください。"}

        records = []
        for page_name, img_path in pages:
            if not os.path.exists(img_path):
                records.append({
                    "page": page_name, "file": os.path.basename(img_path),
                    "error": "画像ファイルが見つかりません",
                })
                continue
            with Image.open(img_path) as im:
                w, h = im.size
                dpi = im.info.get("dpi")
            records.append({
                "page": page_name,
                "file": os.path.basename(img_path),
                "w": w, "h": h,
                "ratio": w / h,
                "dpi": f"{float(dpi[0]):.0f}" if dpi else "-",
                "size_kb": round(os.path.getsize(img_path) / 1024, 1),
            })

        ok_records = [r for r in records if "error" not in r]
        dim_counter = Counter((r["w"], r["h"]) for r in ok_records)
        mode_dim, mode_count = dim_counter.most_common(1)[0] if ok_records else ((0, 0), 0)

        if viewport:
            target_ratio = viewport[0] / viewport[1]
            ratio_source = f"viewport指定 {viewport[0]}x{viewport[1]}"
        else:
            target_ratio = mode_dim[0] / mode_dim[1] if mode_dim[1] else 0
            ratio_source = f"viewport指定なし。最頻解像度 {mode_dim[0]}x{mode_dim[1]} の比率で代用"

        issues = []
        if duplicate_ids:
            issues.append(f"content.opf内でmanifest idが重複しています: {duplicate_ids}"
                           " (XML仕様違反。InDesignが全角/特殊文字ファイル名のID生成に失敗している可能性)")

        for r in records:
            if "error" in r:
                issues.append(f"{r['page']} ({r['file']}): {r['error']}")
                r["flags"] = [r["error"]]
                continue
            flags = []
            if (r["w"], r["h"]) != mode_dim:
                flags.append("解像度不一致")
            if target_ratio and abs(r["ratio"] - target_ratio) / target_ratio > tolerance:
                flags.append("縦横比が許容誤差超")
            if not is_ascii_safe(r["file"]):
                flags.append("非ASCIIファイル名")
            r["flags"] = flags
            if flags:
                issues.append(f"{r['page']} ({r['file']}): {', '.join(flags)}")

        spread_warnings = check_spread_structure(spine_properties, generator)

        spreads = group_into_spreads(spine_properties)
        rep_images = collect_representative_images(opf_dir, spine_hrefs)
        preview_thumbs, preview_truncated = build_preview_thumbnails(rep_images, spreads)

        return {
            "total_pages": len(records),
            "viewport": viewport,
            "target_ratio": round(target_ratio, 4) if target_ratio else None,
            "ratio_source": ratio_source,
            "mode_dim": mode_dim,
            "mode_count": mode_count,
            "records": records,
            "issues": issues,
            "spread_warnings": spread_warnings,
            "ok": not issues,
            "preview_thumbs": preview_thumbs,
            "preview_truncated": preview_truncated,
        }
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------- epub生成 (build_epub_from_images.py 相当) ----------

def natural_sort_key(name):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', name)]


def load_and_normalize(path, target_size):
    im = Image.open(path).convert("RGB")
    if target_size and im.size != target_size:
        im = im.resize(target_size, Image.LANCZOS)
    return im


def build_epub_from_images(page_paths, cover_path, title, output_path,
                            direction="rtl", first_side="left", quality=95):
    """page_paths: 表示順に並んだ画像ファイルパスのリスト。cover_pathはNone可。"""
    if not page_paths:
        raise ValueError("本文ページ画像が1枚もありません。")

    sizes = []
    for p in page_paths:
        with Image.open(p) as im:
            sizes.append(im.size)
    target_size = Counter(sizes).most_common(1)[0][0]
    W, H = target_size

    work_dir = tempfile.mkdtemp(prefix="epub_build_")
    try:
        xhtml_dir = os.path.join(work_dir, "item", "xhtml")
        style_dir = os.path.join(work_dir, "item", "style")
        image_dir = os.path.join(work_dir, "item", "image")
        for d in (xhtml_dir, style_dir, image_dir, os.path.join(work_dir, "META-INF")):
            os.makedirs(d, exist_ok=True)

        with open(os.path.join(work_dir, "mimetype"), "w", encoding="utf-8") as f:
            f.write("application/epub+zip")

        with open(os.path.join(work_dir, "META-INF", "container.xml"), "w", encoding="utf-8") as f:
            f.write('''<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="item/standard.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
''')

        with open(os.path.join(style_dir, "fixed-layout-jp.css"), "w", encoding="utf-8") as f:
            f.write(CSS)

        manifest_items = []
        spine_items = []
        toc_entries = []
        rep_image_paths = []
        spine_properties = []

        if cover_path:
            cover_im = load_and_normalize(cover_path, target_size)
            cover_im.save(os.path.join(image_dir, "cover.jpg"), quality=quality)
            with open(os.path.join(xhtml_dir, "p-cover.xhtml"), "w", encoding="utf-8") as f:
                f.write(PAGE_TMPL.format(title=title, w=W, h=H, img="cover.jpg",
                                          cover_attr=' epub:type="cover"'))
            manifest_items.append('    <item id="p-cover" href="xhtml/p-cover.xhtml" media-type="application/xhtml+xml" properties="svg"/>')
            spine_items.append('    <itemref linear="yes" idref="p-cover" properties="rendition:page-spread-center"/>')
            manifest_items.append('    <item id="cover-img" href="image/cover.jpg" media-type="image/jpeg" properties="cover-image"/>')
            toc_entries.append('<li><a href="xhtml/p-cover.xhtml">Cover</a></li>')
            rep_image_paths.append(os.path.join(image_dir, "cover.jpg"))
            spine_properties.append("page-spread-center")

        sides = ["left", "right"] if first_side == "left" else ["right", "left"]
        for i, src_path in enumerate(page_paths, start=1):
            pid = f"p-{i:03d}"
            im = load_and_normalize(src_path, target_size)
            im.save(os.path.join(image_dir, f"{pid}.jpg"), quality=quality)
            with open(os.path.join(xhtml_dir, f"{pid}.xhtml"), "w", encoding="utf-8") as f:
                f.write(PAGE_TMPL.format(title=title, w=W, h=H, img=f"{pid}.jpg", cover_attr=""))
            manifest_items.append(f'    <item id="{pid}" href="xhtml/{pid}.xhtml" media-type="application/xhtml+xml" properties="svg"/>')
            side = sides[(i - 1) % 2]
            spine_items.append(f'    <itemref linear="yes" idref="{pid}" properties="page-spread-{side}"/>')
            manifest_items.append(f'    <item id="img-{i:03d}" href="image/{pid}.jpg" media-type="image/jpeg"/>')
            rep_image_paths.append(os.path.join(image_dir, f"{pid}.jpg"))
            spine_properties.append(f"page-spread-{side}")

        manifest_items.append('    <item id="nav" href="navigation-documents.xhtml" media-type="application/xhtml+xml" properties="nav"/>')
        manifest_items.append('    <item id="css" href="style/fixed-layout-jp.css" media-type="text/css"/>')

        opf = f'''<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" xml:lang="ja" unique-identifier="unique-id" prefix="ebpaj: http://www.ebpaj.jp/ fixed-layout-jp: http://www.digital-comic.jp/ ibooks: http://vocabulary.itunes.apple.com/rdf/ibooks/vocabulary-extensions-1.0/">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf" xmlns:dcterms="http://purl.org/dc/terms">
    <dc:title id="title">{title}</dc:title>
    <dc:identifier id="unique-id">urn:uuid:{os.urandom(16).hex()}</dc:identifier>
    <dc:language>ja</dc:language>
    <meta property="rendition:layout">pre-paginated</meta>
    <meta property="rendition:spread">landscape</meta>
    <meta property="fixed-layout-jp:viewport">width={W}, height={H}</meta>
    {'<meta name="cover" content="cover-img"/>' if cover_path else ''}
    <meta name="fixed-layout" content="true"/>
    <meta name="primary-writing-mode" content="{'horizontal-rl' if direction == 'rtl' else 'horizontal-lr'}"/>
  </metadata>
  <manifest>
{chr(10).join(manifest_items)}
  </manifest>
  <spine page-progression-direction="{direction}" toc="nav">
{chr(10).join(spine_items)}
  </spine>
</package>
'''
        with open(os.path.join(work_dir, "item", "standard.opf"), "w", encoding="utf-8") as f:
            f.write(opf)

        with open(os.path.join(work_dir, "item", "navigation-documents.xhtml"), "w", encoding="utf-8") as f:
            f.write(f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="ja">
    <head>
        <meta charset="UTF-8" />
        <title>{title}</title>
    </head>
    <body>
        <nav epub:type="toc" id="toc">
            <h1>{title}</h1>
            <ol>
                {''.join(toc_entries)}
            </ol>
        </nav>
    </body>
</html>
''')

        if os.path.exists(output_path):
            os.remove(output_path)
        with zipfile.ZipFile(output_path, "w") as zf:
            zf.write(os.path.join(work_dir, "mimetype"), "mimetype", compress_type=zipfile.ZIP_STORED)
            for dirpath, _, filenames in os.walk(work_dir):
                for fn in filenames:
                    full = os.path.join(dirpath, fn)
                    rel = os.path.relpath(full, work_dir).replace(os.sep, "/")
                    if rel == "mimetype":
                        continue
                    zf.write(full, rel, compress_type=zipfile.ZIP_DEFLATED)

        spreads = group_into_spreads(spine_properties)
        preview_thumbs, preview_truncated = build_preview_thumbnails(rep_image_paths, spreads)

        return {
            "pages": len(page_paths),
            "resolution": f"{W}x{H}",
            "has_cover": bool(cover_path),
            "preview_thumbs": preview_thumbs,
            "preview_truncated": preview_truncated,
        }
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
