"""工事図面管理機能（GUIに依存しない処理）。

- layouts: 図面様式（表題欄・目次の位置）の読込み
- extract: PDFから表題欄・目次・参照番号を読み取る
- checks:  発注前チェックのルール群（python -m drawings.checks で単体実行できる）
- ledger:  工事台帳（JSON）の読み書き
- report:  チェック結果・変更一覧のCSV/HTML出力
"""
