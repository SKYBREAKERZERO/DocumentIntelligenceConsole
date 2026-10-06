# QA 字段映射报告 — v5.9.0-CN

- 来源文件：`20260401_hyoukaQA.xlsx`
- Sheet：`export`
- 列数：**112**
- 一级标准化字段：**59**
- 无损扩展字段：**53**
- 未识别字段策略：`PRESERVE_IN_RAW_JSON_AND_CUSTOM_FIELDS`

## 一级字段映射

| 源字段 | Canonical 字段 |
|---|---|
| `要約` | `summary` |
| `課題キー` | `issue_key` |
| `課題 ID` | `issue_id` |
| `親 ID` | `parent_issue_id` |
| `課題タイプ` | `issue_type` |
| `ステータス` | `status` |
| `プロジェクト キー` | `project_key` |
| `プロジェクト名` | `project_name` |
| `プロジェクトタイプ` | `project_type` |
| `プロジェクト リーダー` | `project_leader` |
| `優先度` | `priority` |
| `解決状況` | `resolution` |
| `担当者` | `assignee` |
| `報告者` | `reporter` |
| `作成者` | `creator` |
| `作成日` | `created_at` |
| `更新日` | `updated_at` |
| `最終閲覧日` | `last_viewed_at` |
| `解決日` | `resolved_at` |
| `コンポーネント` | `component` |
| `期限` | `due_at` |
| `ラベル` | `labels` |
| `説明` | `description` |
| `ウォッチャー` | `watchers` |
| `作業ログ` | `worklog` |
| `添付ファイル` | `attachments` |
| `カスタム フィールド (Watcher/s)` | `watchers` |
| `カスタム フィールド (グレード)` | `grade` |
| `カスタム フィールド (シート名)` | `sheet_name` |
| `カスタム フィールド (ソフトバージョン)` | `software_version` |
| `カスタム フィールド (テスト仕様書不備詳細)` | `test_spec_defect_detail` |
| `カスタム フィールド (テスト仕様書名)` | `test_spec_name` |
| `カスタム フィールド (不具合内容)` | `defect_content` |
| `カスタム フィールド (仕向け)` | `destination` |
| `カスタム フィールド (内容種別)` | `content_type` |
| `カスタム フィールド (前提条件)` | `precondition` |
| `カスタム フィールド (参照仕様書)` | `reference_spec` |
| `カスタム フィールド (回答)` | `answer` |
| `カスタム フィールド (回答区分)` | `answer_type` |
| `カスタム フィールド (回答希望日)` | `answer_requested_at` |
| `カスタム フィールド (変更対象情報)` | `change_target` |
| `カスタム フィールド (実動作)` | `actual_behavior` |
| `カスタム フィールド (対策適用範囲)` | `countermeasure_scope` |
| `カスタム フィールド (影響分析結果)` | `impact_analysis` |
| `カスタム フィールド (影響度)` | `impact` |
| `カスタム フィールド (手順/期待値)` | `procedure_expected` |
| `カスタム フィールド (担当Gr.)` | `responsible_group` |
| `カスタム フィールド (最終回答Gr.)` | `final_answer_group` |
| `カスタム フィールド (最終回答内容)` | `final_answer` |
| `カスタム フィールド (最終回答者)` | `final_answerer` |
| `カスタム フィールド (根拠)` | `rationale` |
| `カスタム フィールド (概要)` | `summary` |
| `カスタム フィールド (機種)` | `model` |
| `カスタム フィールド (発生環境_詳細)` | `occurrence_environment_detail` |
| `カスタム フィールド (発生確率)` | `occurrence_probability` |
| `カスタム フィールド (項目No./セル番地)` | `item_cell` |
| `カスタム フィールド (顧客報告)` | `customer_report` |
| `コメント` | `comments` |
| `壁ID` | `wall_id` |

## 扩展字段（不丢弃）

以下字段不强制映射到一级关系列，但会完整保留到 `raw_record`；Jira Custom Field 同时保留到 `custom_fields`，可通过 `custom[字段]=值` 做参数化过滤。

- `投票`
- `残余見積`
- `消費時間`
- `Σ 残余見積`
- `Σ 消費時間`
- `課題外部リンク (Blocks)`
- `課題外部リンク (Cloners)`
- `課題外部リンク (Duplicate)`
- `課題外部リンク (Relates)`
- `課題外部リンク (終了-終了 依存関係(WBSGantt))`
- `課題外部リンク (終了-開始 依存関係(WBSGantt))`
- `課題外部リンク (親子リンク(WBSGantt))`
- `カスタム フィールド (MSOC Full Version)`
- `カスタム フィールド (MSOC RC Version)`
- `カスタム フィールド (Same Cause Failure)`
- `カスタム フィールド (User Infomation)`
- `カスタム フィールド (デグレ管理_実施状況(過去Ver再現実験))`
- `カスタム フィールド (プロジェクト固有項目)`
- `カスタム フィールド (マイルストン(WBSGantt))`
- `カスタム フィールド (ランク)`
- `カスタム フィールド (ランク (Obsolete))`
- `カスタム フィールド (仕様発行予定日)`
- `カスタム フィールド (仕様発行完了日)`
- `カスタム フィールド (作成部署)`
- `カスタム フィールド (作業割合(WBSGantt))`
- `カスタム フィールド (出荷停止)`
- `カスタム フィールド (品速No.)`
- `カスタム フィールド (実施可能日目途)`
- `カスタム フィールド (対象Jenkins Job)`
- `カスタム フィールド (対象Jenkins Job(16DOP))`
- `カスタム フィールド (待機状態)`
- `カスタム フィールド (必要機材・機能名称)`
- `カスタム フィールド (必要機材・機能詳細)`
- `カスタム フィールド (手動スケジュール(WBSGantt))`
- `カスタム フィールド (拠点展開チェック)`
- `カスタム フィールド (拠点展開状況)`
- `カスタム フィールド (横展開(ETC/V2X))`
- `カスタム フィールド (横展開(IVI/DSM))`
- `カスタム フィールド (横展開(Map))`
- `カスタム フィールド (横展開(Meter/HUD))`
- `カスタム フィールド (横展開(TCU))`
- `カスタム フィールド (着手指示ステータス)`
- `カスタム フィールド (管理対象フラグ)`
- `カスタム フィールド (背反の検討結果)`
- `カスタム フィールド (進捗率[CT])`
- `カスタム フィールド (進捗率[IT])`
- `カスタム フィールド (進捗率[MK])`
- `カスタム フィールド (進捗率[PD])`
- `カスタム フィールド (進捗率[RA])`
- `カスタム フィールド (進捗率[SD])`
- `カスタム フィールド (過去トラチェック)`
- `カスタム フィールド (過去トラ登録状況)`
- `カスタム フィールド (開始日)`
