# Movie2Motion 0.4.0

Blenderあにめ　Mixamoで満足できないあなたへ

MP4またはMOV動画に映る人型1人の全身姿勢をローカルで推定し，Pinnacle-Riggerで生成したアーマチュアにBlender Actionを作るアドオンです。MediaPipeの3Dランドマークから胴体・頭・腕・手首・脚・足の回転を求めます。リグのレスト姿勢からボーン軸を読み取るため，同じ命名のボーンに回転値をそのまま流し込む方式とは結果が異なります。

## 初回セットアップ

Blender 4.2以上を対象にしています。LinuxのBlender 4.5.3 LTSと5.2.2 LTS，外部Python 3.12で動作確認しました。

1. このフォルダで，動画解析用のPython環境を用意します。

   ```bash
   python3.12 -m venv .venv
   .venv/bin/python -m pip install -r requirements.txt
   curl -fL https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_full/float16/1/pose_landmarker_full.task -o pose_landmarker_full.task
   ```

   精度を優先する場合は，解析は遅くなりますが heavy モデルを使えます。アドオン設定の「MediaPipe の .task モデル」でこちらを指定してください。

   ```bash
   curl -fL https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_heavy/float16/1/pose_landmarker_heavy.task -o pose_landmarker_heavy.task
   ```

2. `python3 build.py` でアドオンのZIPを作り，Blenderの「編集 → プリファレンス → アドオン → ディスクからインストール」で `dist/movie2motion-0.4.0.zip` を選び，**Movie2Motion** を有効にします。
3. アドオン設定の「外部 Python の実行ファイル」に `.venv/bin/python`，「MediaPipe の .task モデル」に `pose_landmarker_full.task` を指定します。各ファイルのフルパスを選んでください。パスはBlenderのユーザー設定フォルダにある `movie2motion-runtime.json` に保存され，同じBlender環境でアドオンを更新・再インストールしても自動で読み込まれます。動画の解析時はネット接続不要です。

## 動画からActionを作る

1. Pinnacle-Riggerで生成済みのアーマチュアを選び，生成時にタイムラインをフレーム1へ移し，Actionもフレーム1から開始します。
2. 3Dビューで `N` を押し，**Movie2Motion** タブでMP4またはMOVを選びます。最初は全身が映る1人の短い動画が扱いやすいです。
3. **動画から Action を生成** を押します。解析中は `Esc` でキャンセルできます。生成したActionはリグに割り当てられ，Action Editorで編集できます。それまで割り当てていたActionも保存されます。

解析FPSは動画をサンプリングする頻度です。キーフレーム位置はフレーム1を起点に，動画の時刻とシーンのFPSから決まります。平滑化を有効にすると，前後の検出が約0.2秒以内にある短い欠落を補間し，前後のフレームから関節位置を平滑化してからボーン回転を求めます。長い欠落では直前の回転を保持します。平滑化の既定値は0.08秒で，値を上げると細かい動きも丸くなります。0にすると補間と平滑化を無効にできます。処理順は [FreeMoCapの後処理](https://docs.freemocap.org/tutorials/post-processing/) を参考にしています。

### 肘・膝・手首のひねり

0.4.0から，腕と脚の向きに加えて，ボーン軸まわりのひねりも動画から求めます。

- 上腕と太ももは，肘や膝が曲がる面に合わせてひねります。肘は前，膝は後ろに曲がる前提です。曲がりが約8度より小さいと曲がる面を決められないため，約25度までのあいだで徐々に効かせます。
- 手首は人差し指と小指の付け根の並びから手のひらの向きを求めます。ひねりの半分を前腕に分け，ツイストボーンのないリグでも手首のメッシュがつぶれにくくしています。
- 検出の誤りで関節が裏返らないよう，ひねり量に上限を設けています。

### 足の接地補正

「足の接地補正」を「両足固定」にすると，構えなど両足をついたクリップ全体でつま先を固定します。「自動」では，低い位置にあり，ゆっくり動く足の接地区間を推定して，その前後で補正を滑らかに切り替えます。推定は水平な床を前提とし，足の遮蔽や階段では精度が下がります。Pinnacle-Riggerのレスト姿勢のつま先の高さを接地面の基準にし，膝・足首の回転と腰の上下移動をActionに焼き込みます。

## 試作の範囲

- 1人の姿勢推定に対応します。複数人が交差する場面で同じ人物を追い続ける機能や，映像のキャラクターとBlenderモデルを自動で照合する機能はありません。
- 腰の移動は，接地補正に伴う上下調整に対応します。単眼映像からの奥行きは推定値で，遮蔽，急なカメラ移動，極端にデフォルメされた人型では検出が不安定になります。
- 指・表情・補助ボーンのアニメーションは未実装です。対象リグはPinnacle-Riggerのボーン名と階層を前提にしています。

姿勢モデルの詳細は [Google MediaPipe Pose Landmarker](https://developers.google.com/edge/mediapipe/solutions/vision/pose_landmarker?tab=Python) を参照してください。

ZIPの再生成は `python3 build.py` で行えます。

## ライセンス

[Apache License 2.0](LICENSE)
