# Tasks: 工事図面管理

## Phase 1: 発注前チェックのエンジン（GUIなし）

- [X] T001 試作スクリプトを `tools/` に置き、実図面で仕様書 §3 の結果が再現できることを確認する。
- [X] T002 実図面から間違い3か所を仕込んだ版を作るツール `tools/make_error_sample.py` を作る。
- [X] T003 `layouts/toshiseibi_A1.json` と `drawings/layouts.py`。
- [X] T004 `drawings/extract.py`（表題欄・目次・参照番号）。
- [X] T005 `drawings/checks.py`（16ルールとコマンドライン）、`drawings/report.py`（CSV/HTML）。
- [X] T006 `drawings/ledger.py` の台帳形式（§5）。
- [X] T007 テスト（合成PDF・実図面・A3縮小版・試作との一致）。

## Phase 2: 図面セットタブ

- [X] T008 `DrawingSetTab`（`BasePdfTab` 継承）と赤枠表示の `HighlightPdfView`。
- [X] T009 開始画面・タブの右クリックメニューからの入口、様式の選択。
- [X] T010 CSV・HTMLレポート出力、「要確認のみ」、絞り込み、スクロールとの同期。

## Phase 3: 変更記録台帳

- [X] T011 工事フォルダを開く／台帳の新規作成（発注図から図面一覧と版を作る）。
- [X] T012 図面一覧と図面別の変更記録（時系列）、変更記録の一覧（状態・図面・期間で絞り込み）。
- [X] T013 変更記録フォーム（PDF上のドラッグで箇所を登録）、履歴の自動記録、保存時のバックアップと更新検知。

## Phase 4: 変更契約・竣工図

- [X] T014 変更契約用一覧（HTML/CSV）。
- [X] T015 変更図面の登録（チェックつき）と、反映した記録の状態更新。
- [X] T016 新旧対照ウインドウ。
- [X] T017 竣工図チェックリスト（チェックで状態更新、HTML/CSV）。

## Verification

- 2026-09-29: `python -m unittest discover -s tests -t .` — 44件中43件成功。失敗1件（`test_message_attachments.test_unresolved_chip_is_red_and_not_clickable`）は本機能の着手前から失敗していたもので、本機能とは無関係。
- 2026-09-29: 実図面（A1・11ページ）と間違い仕込み版で、仕様書 §4.5 の合格条件をすべて満たし、試作スクリプトと同じ結果になることをテストで確認。A3縮小版（全ページを約1/2に縮小したPDF）でも同じ読み取り結果。
- 2026-09-29: オフスクリーン描画の画面キャプチャで、図面セットタブ（指摘クリックで拡大・赤枠）、工事台帳タブ、変更記録フォーム（ドラッグで箇所登録）、変更図面の登録、竣工図チェックリスト、新旧対照を確認。`python main.py` 相当の起動（モジュール名 `__main__`）で図面セットタブが開けることを確認。
- 未確認: 実機のWindows画面での手動操作、Nuitkaビルド・MSI（`build_nuitka.bat` に `layouts/` 等の同梱指定を追加済み）。
