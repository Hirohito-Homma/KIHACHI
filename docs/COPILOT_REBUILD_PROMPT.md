# KIHACHI Music AI — GitHub Copilot 再実装仕様

この文書をGitHub Copilot Agentへそのまま渡し、`kihachi-music-ai`をゼロから再実装する。既存実装の局所的な写経ではなく、下記の契約・README・ADRを正本として、各フェーズでテストを通しながら進める。

## 0. 最重要制約

1. コアはPython標準ライブラリのみで動く（`pyproject.toml`の通常依存は空）。
2. 完全決定論：同じ入力・seed・バージョンは同じSongSpec、MIDI、JSONを生成する。
3. `Music Brain → SongSpec → Composer/Compiler`を唯一の主経路とし、SongSpecを共有契約にする。
4. ACE-Step、AbletonGPT、LLM、stem分離はアダプター境界の外側。コアへGPU・サーバー・秘密鍵を持ち込まない。
5. 既存Audio、MIDI、解析、レビュー成果物は読み取り専用。出力先が存在する場合は`--overwrite`なしで停止する。
6. 推定値には`confidence`、`reliable`、coverageを添え、「未計測」と「問題なし」を混同しない。
7. 解析値だけで生成takeを自動採用しない。比較・レビュー・人間の採用判断を残す。
8. 新しいJSONフィールドは後方互換なoptionalとし、既存のダイジェストを不要に変えない。

## 1. 正本と成果物

- `README.md`: CLI、入出力、v0.1/v0.2境界、実測限界。
- `docs/adr/0001-standalone-core.md`〜`0014-swing-warps-the-beat.md`: 設計判断。
- 実装の入口は`src/kihachi_music_ai/`、テストは`tests/`。
- 既存生成物は保存し、新しい成果物は sibling pathへ書く。SHA-256を記録する。

## 2. アーキテクチャ

```
brief text
  -> MusicBrain / intent readers
  -> SongSpec (唯一の契約)
  -> Composer (各section/trackを決定論的に生成)
  -> MIDI + prompt.txt + prompt.json
  -> optional adapters: ACE-Step / WAV analyzer / Ableton plan / LLM / stems
```

SongSpecはdataclassで検証し、BPM、キー、拍子、尺、ジャンル、groove、harmony、arrangement、active tracksを保持する。セクション固有の乱数は`seed:track:section_index`等の独立ストリームから取得し、1セクションの編集が他セクションのMIDIを変えない。

## 3. 実装フェーズ

1. SongSpec dataclass、JSON round-trip、検証。
2. deterministic Music Brain、ジャンルDB、キー/コード理論。
3. arrangement、groove、Composer、MIDI format 0 / 480 PPQ。
4. prompt compiler（`prompt.txt`/`prompt.json`）とCLI基盤。
5. WAV analyzer（PCM、BPM、キー、chroma、section energy）とconfidence。
6. MIDI readback、midi-review、レビュー境界。
7. edit/apply-edit、revision/repaint計画、上書き拒否。
8. chunk plan/render、tail guard、resumeと全小節被覆。
9. material sampler、stem import/prepare、Audio-to-MIDI（単音素材のみ）。
10. Ableton plan adapter（Live操作は計画出力のみ、実機適用は別プロジェクト）。
11. optional LLM/ACE-Step adapters。秘密情報は環境変数のみ。

## 4. 音楽・解析上の必須意味論

- 日本語否定は直前の言及、英語否定は直後の言及へ結び、節境界を越えない。
- refusalは反対方向への要求ではなく、パラメータの低い極で止める。
- swingは0.5〜0.66の範囲、note lengthは既存定数比で扱う。
- 完成mixからのコード推定は設計の真実ではない。SongSpec MIDIを照合の正本とする。
- 単音Audio-to-MIDIは音高をpitch tracker、時刻をonsetへ分離し、coverageを必ず保存する。フルミックスや和音を推測で埋めない。

## 5. 必須CLIと安全規則

READMEに記載された`compose`、`analyze`、`review`、`edit`、`apply-edit`、`render-chunks`、`cut-sample`、`transcribe-sample`、`audit-transcription`、`stems prepare/import`、`ableton-plan`等を実装する。各コマンドは入力不備を短いエラーにし、元Audioや既存成果物を変更しない。manifestの版数、パス、SHA-256、BPM、キーを検証する。

## 5.1. 実装ファイルの責務

必須の責務境界は次の通り。ファイルを統合・改名する場合も、公開CLIとSongSpec契約を維持する。

```text
models.py / music_brain.py / intent.py / genres.py / theory.py  # 契約と解釈
arrangement.py / groove.py / mutation.py / composer.py / midi.py # 作曲とSMF
pipeline.py / prompt_compiler.py / derive.py                    # 出力経路
analyzer.py / spectrum.py / loudness.py / defects.py             # WAV測定
reviewer.py / revision.py / repaint_planner.py / decision.py     # レビュー境界
sampler.py / stems.py / transcribe.py                            # 素材・stem・転写
ableton.py / instrumental.py                                     # 外部Live計画
adapters/ace_step.py / adapters/intent_llm.py                    # 外部サービス
cli/parser.py / cli/_legacy.py                                   # CLI配線
```

## 6. テストと完了条件

- `python -m unittest discover -s tests` と `python -m pytest -q` が通る。
- SongSpec/MIDIの決定論、section局所性、golden hash、JSON round-tripを固定する。
- 否定・度合い・英語境界・section scopeを語彙スイープで検査する。
- WAV形式、manifest破損、SHA不一致、上書き、path traversalを拒否する。
- Audio-to-MIDIは合成単音、反復音、無音、ステレオ平均、非16-bit、CLI、coverage JSONを検査する。
- 実機/外部サービスを使うテストは、コードテストと分離し、測定結果・confidence・未検証項目を記録する。

実装を開始する前にREADMEと全ADRを読み、各フェーズ完了時に対象テスト、差分、保護したファイル、未解決の限界を報告する。仕様にない自動採用・破壊的変更・外部送信を追加してはならない。
