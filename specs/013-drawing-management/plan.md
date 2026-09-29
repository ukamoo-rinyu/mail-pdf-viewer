# Implementation Plan: 工事図面管理

## 構成

| ファイル | 役割 | GUI依存 |
| --- | --- | --- |
| `drawings/layouts.py` | 様式設定 `layouts/*.json` の読込み。座標をページの大きさとの比率で換算 | なし |
| `drawings/extract.py` | 表題欄（`TitleBlock`）・目次（`IndexEntry`）・凡例の参照番号（`Reference`）の読み取り | なし |
| `drawings/checks.py` | 判定ルール（1ルール1関数、結果は `Finding`）とコマンドライン | なし |
| `drawings/ledger.py` | 工事台帳JSONの読み書き・バックアップ・更新検知・履歴・変更契約/竣工図の集計 | なし |
| `drawings/report.py` | チェック結果・変更一覧・チェックリストのCSV/HTML出力 | なし |
| `drawing_tab.py` | 図面セットタブ、工事台帳タブ、変更記録フォーム、新規台帳、変更図面登録、新旧対照 | PySide6 |
| `main.py` | 開始画面のボタン2つ、タブの右クリックメニュー2項目、`MainWindow.open_drawing_set/open_ledger_folder`、`sys.modules["main"]` の登録、読み込み件数の単位 | 既存 |

## 試作からの変更点

- 座標を様式設定に外出しし、比率で換算（A3縮小版に対応）。
- 文字の読み取りを「行」単位から、行の中の「欄の中から始まる文字」単位に補強（隣の欄の文字と1行にまとめられた場合に、欄の外の文字を拾わない・欄の中の文字を落とさない）。実図面では試作と同じ結果。
- 名称不一致の「番号の付け違い？」は別の指摘にせず、名称不一致の文中に併記。

## データへの影響

- 図面セットの表示では、PDFもインデックスDBも書き換えない（`*.index.sqlite3` も作らない）。
- 工事台帳は工事フォルダの `工事台帳.json` と、保存時のバックアップ `工事台帳.bak.json` だけを書く。書き込みは一時ファイル経由で置き換える。
- PDFが工事フォルダの外にある場合は、利用者の了承を得てフォルダ内へコピーする（元のPDFは変更しない）。

## 確認方法

- `tests/test_extract.py`・`tests/test_checks.py`・`tests/test_ledger.py`: 合成PDFと手組みのデータ（常に実行）、実図面（`tests/data/` にあるときだけ）。
- `tests/test_drawing_tab.py`: オフスクリーンで画面を操作（図面セットタブ・既存モード・工事台帳の一連の流れ）。
- オフスクリーン描画の画面キャプチャで見た目を確認。
