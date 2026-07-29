# Rust Type-3 Code Clone Dataset Generator

Rustオープンソースプロジェクトから抽出した関数およびメソッドを用いて，Type-1からType-3までのコードクローン対を含む評価用データセットを構築するプログラムです．

## 必要なパッケージ

次のコマンドで必要なPythonパッケージをインストールします．

```bat
py -m pip install tree_sitter tree_sitter_rust
```

## 入力データの準備

本プログラムの実行には，各Rustプロジェクトから抽出した関数およびメソッドの情報が必要です．

関数およびメソッドの抽出には，[Rust Type-3 Code Clone Detector](https://github.com/sasano-lab-papers/Rust-type3-clone-detector)の`function_extractor`を使用します．

```bat
cargo run --release ^
  --manifest-path crates\function_extractor\Cargo.toml ^
  -- "<Rustプロジェクトのパス>" "<出力先>\functions_rust.jsonl"
```

複数のRustプロジェクトを使用する場合は，プロジェクトごとに抽出処理を行い，次のように保存します．


## 実行方法

必要なPythonパッケージをインストールします．

```bat
py -m pip install -r requirements.txt
```

各Rustプロジェクトから関数およびメソッドを抽出した後，次のコマンドを実行します．

```bat
py build_rust_clone_dataset.py ^
  --source-root "<Rustプロジェクト群のディレクトリ>" ^
  --result-root "<抽出結果のディレクトリ>" ^
  --out-dir "<出力先ディレクトリ>" ^
  --target-low-negatives 30000 ^
  --target-similar-negatives 30000
```

- `--source-root`：解析対象となるRustプロジェクト群を格納したディレクトリを指定します．
- `--result-root`：各Rustプロジェクトから抽出した関数およびメソッドの情報を格納したディレクトリを指定します．
- `--out-dir`：構築した評価用データセットの出力先ディレクトリを指定します．
- `--target-low-negatives`：低類似度負例対の選定数を指定します．デフォルトは0です．
- `--target-similar-negatives`：一定の類似性を持つ負例対の選定数を指定します．デフォルトは0です．

指定した数の負例対を選定できない場合は，条件を満たす候補の範囲内で可能な限り選定します．
負例対を選定しない場合は，これら2つのオプションを省略できます．

## 主な出力ファイル

| ファイル | 内容 |
|---|---|
| `generated_project/src/*.rs` | 生成したRustコード |
| `dataset_functions.jsonl` | 関数およびメソッドの情報 |
| `dataset_pairs.csv` | 正例対（`label=1`）および負例対（`label=0`） |
| `mutation_log.jsonl` | 適用した変換の記録 |
| `summary.json` | 生成結果の集計 |

`dataset_pairs.csv`では，正例クローン対を`1`，負例対を`0`として記録します．
