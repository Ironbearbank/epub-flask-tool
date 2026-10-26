# epub-flask-tool

marcheのフォトブック(epub)向け、簡易Webツール。

- **epubチェック**: 入稿epubの画像解像度・縦横比・ファイル名・見開き構成をチェック
- **epub生成**: 個別ページ画像(+任意の表紙画像)から、marche_viewerで安定動作する
  SVG埋め込み形式のepubを新規生成

`pdf-flask-tool`(オンラインサイン会のPDF差し替えツール)と同じ構成・デプロイ方針。

## ローカル実行

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/python3 app.py
# http://127.0.0.1:5050 を開く
```

## デプロイ(Render)

1. このリポジトリをRenderの「New Web Service」でGitHub連携
2. Build Command: `pip install -r requirements.txt`
3. Start Command: `gunicorn app:app`
4. 環境変数 `SECRET_KEY` を適当な値で設定(flashメッセージ用)
5. 写真集epubはファイルサイズが大きくなりやすいため、Renderのプラン上限
   (リクエストサイズ・メモリ)を確認しておくこと。`app.py` 側は512MBまで許容。

## 背景・技術メモ

なぜ画像埋め込みに`<svg><image xlink:href=.../></svg>`形式を使っているかなど、
設計の背景は `epub_core.py` のdocstring、および社内メモリ
`project_epub_indesign_vs_svg_compat` を参照。
