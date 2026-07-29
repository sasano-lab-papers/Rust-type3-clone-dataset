# Rust Type-3 Code Clone Dataset Generator

Rustオープンソースプロジェクトから抽出した関数およびメソッドを用いて，Type-1からType-3までのコードクローン対を含む評価用データセットを構築するプログラムです．

## 必要なパッケージ

次のコマンドで必要なPythonパッケージをインストールします．

```bat
py -m pip install tree_sitter tree_sitter_rust
```

## 実行方法

Windowsのコマンドプロンプトで，リポジトリのルートディレクトリから次のコマンドを実行します．

```bat
py build_rust_clone_dataset.py ^
  --source-root "<Rustプロジェクト群のディレクトリ>" ^
  --result-root "<関数およびメソッドの抽出結果のディレクトリ>" ^
  --out-dir "<出力先ディレクトリ>" ^
  --num-seeds 0 ^
  --positive-pair-mode original_only ^
  --target-low-negatives 30000 ^
  --target-similar-negatives 30000
```

- `--source-root`：Rustプロジェクト群を格納したディレクトリ
- `--result-root`：関数およびメソッドの抽出結果を格納したディレクトリ
- `--out-dir`：生成結果の出力先ディレクトリ

## 出力ファイル

| ファイル | 内容 |
|---|---|
| `generated_project/src/*.rs` | 生成した関数およびメソッドを格納したRustファイル |
| `dataset_functions.jsonl` | データセットに含まれる関数およびメソッドの情報 |
| `dataset_pairs.csv` | 正例クローン対と負例対を含むラベル付きペア |
| `selected_seeds.jsonl` | seedとして選定された関数およびメソッドの情報 |
| `mutation_log.jsonl` | 適用した変換と生成結果の記録 |
| `seed_rejections.csv` | seedとして選定されなかった候補とその理由 |
| `summary.json` | 生成数や変換種別ごとの集計結果 |

`dataset_pairs.csv`では，正例クローン対を`1`，負例対を`0`として記録します．
