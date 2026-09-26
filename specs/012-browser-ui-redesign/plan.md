# Implementation Plan: ブラウザ版に合わせた画面・表示設定・リンク操作

## Context

本家 mail-pdf-viewer（`release/v1.0.0`）で行った画面刷新は、kojiPDFviewer の分岐元（`67adb9b`）以降に両方で `main.py`・`db.py`・`parser.py` を変更しているため、そのままではマージできない。本家の新しい `main.py` を土台に、kojiPDFviewer の機能を移植する。

## Design

1. `db.py`・`parser.py` は kojiPDFviewer の版を使う（添付の照合・索引の版・階層対象）。本家側の添付名照合と「添付の先頭へ移動」は取り込まない。
2. `main.py` は本家の画面構成（タブ最上部、一覧の上の検索欄と表示設定、PDF表示の上のバー）に、kojiPDFviewer の次の部品を組み込む。
   - `PageNumberControl` を各タブのPDF表示の上のバーと別ウインドウに置く。Ctrl+G で入力欄へ移る。
   - `LinkedPdfView` に本家のリンク検出（本文中のURL・メールアドレス）、マウスを乗せたときの強調表示、表示切り替え・大きさ変更時のページ保持を加える。クリック時の処理は kojiPDFviewer の `_open_external_link` を使う。
   - メール一覧の「しおり一覧へ」、対象・範囲の選択、資料行、しおり一覧の「メール一覧へ」と右クリックの対象指定、「全展開 ＋ 数字 − 全折り」。
   - 対応付けられない添付の赤表示と、押しても行が選択されない動作。
   - アプリ名・アイコン・旧設定の引き継ぎ。
3. 一覧の表示設定は `ViewSettings` で保存し、`SettingsPanel` で表示する。

## Data and compatibility

PDF、索引DB、既読状態の形式は kojiPDFviewer のまま変えない。表示設定だけを新たに `QSettings` へ保存する。`VERSION` を 0.4.0 に上げる。
