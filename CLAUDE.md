# epub-flask-tool について

marcheのフォトブック(epub)入稿を対象にした、QAチェック/新規生成/再生成のWebツール。
詳細はREADME.mdを参照。このファイルは、Claude Codeでこのリポジトリを開いた人が
過去の試行錯誤を繰り返さずに済むよう、調整作業で判明した知見をまとめたもの。

## このリポジトリで作業するときの基本姿勢

- ユーザーはPM(エンジニアではない)。実機(marche_viewer)でどう見えるかを最優先の
  判断基準にする。ロジック上の正しさよりも「実機で確認したか」を重視する。
- epub周りの不具合調査では、**ユーザーが実機で撮影した動画**(見開きの実際の見え方)
  がある場合、それを一次情報として扱う。コード側の思い込みで「こう見えるはず」と
  判断しない。
- 変更はこのリポジトリのmain branchにpushするとRenderに自動デプロイされる
  (`https://epub-flask-tool.onrender.com`)。pushする前に挙動を一言で説明し、
  必要ならユーザーの確認を取る。

## marche_viewer特有の既知の落とし穴

1. **InDesign書き出しepubは、表紙追加やページ並び替えに弱い。**
   InDesignが出力する`idGeneratedStyles.css`クラス依存のマークアップ
   (`<div><img class="_idGenObjectAttribute-..." src=... /></div>`)は、
   表紙(page-spread-center)を追加したり、spine/manifestのitemref順を入れ替えたりすると
   marche_viewer側で見開きペアリングが崩れる(無関係なページ同士がペアになる、
   データにない黒ページが挿入される等)。spineのleft/right指定値をどう変えても
   再現し、プロパティ値では制御できなかった。
   - **対処**: calibre/EBPAJ方式(`<svg><image xlink:href="../image/X.jpg" /></svg>`で
     1ページ1画像を埋め込む、classベースの位置指定なしの単純な構造)で作り直すと安定する。
     このツールの「epub生成」「epub再生成」機能(`epub_core.build_epub_from_images`/
     `rebuild_epub_from_existing`)はこの方式で出力する。
   - ページ順を調整したい場合も、spine/manifestのitemref順そのものを変えるのではなく、
     **参照する画像ファイルの中身だけを入れ替える**方が安全(itemref順の変更は別の
     黒ページバグを誘発した実績あり)。

2. **spineの`page-spread-left`/`page-spread-right`と、画面上の物理的な左右は逆転する。**
   RTL(右開き)のページめくり挙動に従うため、プロパティ名`left`のitemは画面**右**に、
   `right`のitemは画面**左**に表示される(実機動画で確認済み)。見開き構成を扱う
   コードを書くときは必ずこの前提を踏まえる。
   `epub_core.build_preview_thumbnails`はペア内で`reversed(spread)`することで
   この逆転を再現している。

3. **携帯(スマホ)では見開きペアリングという概念自体が存在しない。**
   PCのmarche_viewerは2ページを1見開きとして横並び表示するが、携帯では
   spine順に1アイテムずつ、そのページ画像自身のアスペクト比のまま表示される
   (横長の画像ならそのまま横長の1ページとして出る。2枚を合成して見開きにする
   ことはない)。携帯の表示領域(ブラウザのUI分を除いた実高さ)は多くの画像の
   アスペクト比より縦に短いため、縦長ページでも上下に黒帯が入ることがある。
   `epub_core.build_mobile_preview_thumbnails`はこの前提で1枚ずつサムネイル化する。

4. **marcheアップロード用のファイル名はASCII推奨。**
   日本語等の非ASCII文字を含むepub/画像ファイル名は、marcheへのアップロード時に
   エラーの原因になりやすい。このツールで生成/再生成した出力ファイル名は
   `app._safe_filename_stem()`で明示的にASCII(英数字・半角スペース・_-)のみに
   制限している。**Pythonの`str.isalnum()`は日本語等の非ASCII文字にも`True`を返す
   ため、ASCII判定には使えない**(`str.isascii()`と組み合わせる必要がある)。
   なお、①のチェック機能(アップロード/チェック段階)自体は日本語ファイル名でも
   問題なく動作する。英数字化が必要なのは②③で生成/再生成した**出力ファイル**のみ。

5. **DPR(デバイスピクセル比)≠1の環境で画像がクロップされる既知の不具合がある。**
   詳細は社内メモリ`marche_viewer`を参照(原因特定済み、この repo の対象範囲外)。

## epub_core.py の構成

- `run_qa_check(epub_path)`: epubを展開し、全ページの解像度・比率・ファイル名・
  spine構成をチェック。見開き/携帯の両プレビューサムネイルも生成して返す。
- `build_epub_from_images(page_paths, cover_path, title, output_path, ...)`:
  個別ページ画像(+任意の表紙)から、calibre/EBPAJ方式のSVG埋め込みepubを新規生成。
  解像度は最頻出サイズに統一される。
- `rebuild_epub_from_existing(epub_path, output_path, title_override=None, ...)`:
  チェックNGだった既存epubから代表画像を自動抽出し、`build_epub_from_images`で
  解像度等を正規化して再生成する救済導線。表紙の有無はspine先頭の
  `page-spread-center`で判定。
- `group_into_spreads(spine_properties)`: center=単独、left直後にrightが続く場合のみ
  ペア、それ以外は単独、というシンプルなルール。「表紙直後の数ページは必ず単独」
  といった特例は過去に試して誤りだったため入れていない(落とし穴1・2と合わせて確認)。

## デプロイ環境のクセ(Render無料プラン)

- 無料プランのため**コールドスタートがある**(アクセスが一定時間ないとスリープし、
  次のアクセスで30〜60秒ほど起動待ちになる)。重要な場面(デモ・会議等)で使う前は
  事前に一度アクセスして起こしておくことを推奨。
- メモリが512MB上限。画像処理(`epub_core._load_small_thumb`)はフル解像度を
  メモリ展開しないよう`Image.draft()`+段階的`thumbnail()`で縮小してから読み込む
  実装にしている。新しい画像処理を追加する際もこの制約を踏まえる。
- `Procfile`は`--worker-class gthread --threads 3`(スレッド並行)。無料プランの
  メモリ制約上、`--workers`を増やす(プロセス複製)はOOMリスクが高いため避けている。
