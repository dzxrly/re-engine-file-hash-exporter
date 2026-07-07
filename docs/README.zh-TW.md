# RE File Hash Exporter

[English](../README.md) | [简体中文](README.zh-CN.md) | [繁體中文](README.zh-TW.md)

`RE File Hash Exporter` 是一個用於 RE Engine DMP / PAK 工作流程的 `config.toml` 建構工具。

它會從指定的 `.DMP` 檔案中掃描資源路徑，匯出已知的數字版本後綴，提示缺少版本號的路徑，並可選擇使用 PAK hash 證據探索缺失後綴。

## 功能

- 從一個精確指定的 `.DMP` 檔案掃描 UTF-16LE 資源路徑。
- 第一步匯出 DMP 中已經帶有數字後綴的 `suffix_map`。
- 將 DMP 中只有 `name.ext`、沒有 `.version` 的 raw path 按副檔名彙總並提示。
- 第二步允許手動選擇副檔名，並在候選範圍內探索版本號後綴。
- 提供 `auto_detect` 候選規劃模式，可從根目錄 `file_suffix_profiles.json` 讀取可編輯預設。
- 第二步可選顯示已經存在後綴的副檔名，用於繼續搜尋可能新增的版本後綴。
- 後綴探索只讀取 PAK 中繼資料 hash，不解包 PAK 內容。
- 支援批次加入多個 PAK 檔案。
- 當 PAK 檔案未變更時，重複執行 Step 2 會重用已讀取的 PAK 中繼資料快取。
- CPU 模式使用多行程平行匹配。
- CPU 搜尋會依 PAK 群組重用 worker，並重用預先計算的 UTF-16 hash 前綴狀態。
- 勾選 `GPU acceleration (CUDA only)` 且安裝 CUDA 可用的 `torch` 時，會使用 torch CUDA 加速。
- 選擇或偵測到多張 CUDA 顯示卡時，可平行使用多卡加速。
- GPU 搜尋會批次處理預編碼的 UTF-16 候選片段，減少完整路徑字串建構與重複編碼。
- 只有啟用 GPU 模式時才顯示 GPU 專用設定。
- 後綴探索過程中可隨時點擊 `Stop` 停止搜尋。
- 掃描或後綴探索執行時會鎖定檔案輸入和任務選項，避免中途修改輸入。

## 依賴

安裝 `requirements.txt` 中列出的 Python 套件：

```powershell
pip install -r requirements.txt
```

`torch` 只有在需要 GPU 加速時才是執行階段必需項。若要使用 CUDA 加速，請安裝與本機 NVIDIA 驅動和 CUDA 環境相符的 CUDA 版 PyTorch。

## 執行

```powershell
python main.py
```

## CLI 模式

CLI 模式會讀取指定的 TOML 配置檔，不開啟 GUI，直接依序執行 Step 1 和 Step 2：

```powershell
python main.py --cli <config-file.toml>
# 或
python main.py cli <config-file.toml>
```

配置裡的相對路徑都以配置檔所在目錄為基準解析。`output_path` 未填寫時，預設寫到同目錄的 `config.toml`。
執行 Step 2 時，CLI 會使用 Rich 在終端底部固定顯示進度條，上方繼續滾動列印日誌。
Step 2 執行時按一次 `Ctrl+C` 會請求優雅停止，已經找到的部分匹配會合併寫回輸出配置，行為等同 GUI 的 `Stop`；再次按 `Ctrl+C` 才會強制中斷。

完整範例：

```toml
dmp_path = "dump.DMP"
output_path = "config.toml"
run_step2 = true

pak_paths = []
pak_dirs = ["*.[pP][aA][kK]"]
pak_glob = "*.[pP][aA][kK]"

[step2]
selected_extensions = "all_missing"
mode = "auto_detect"
min_version = 0
max_version = 4096
custom_versions = ""
neighbor_radius = 32
date_start = "0"
date_end = "today"
processes = 0
include_platform_suffixes = true
language_mode = "localized"
include_streaming = true
include_versioned_extensions = false
request_gpu = false
gpu_batch_size = 16384
gpu_devices = "auto"
gpu_workers_per_device = 1
gpu_batch_sizes = ""
```

建議結構：輸入、輸出和 PAK 選擇寫在頂層，後綴探索選項寫在 `[step2]`。Step 2 選項也可以放在頂層；相容舊配置的 `[bruteforce]` 表也會被讀取，但同時存在時 `[step2]` 優先生效。

頂層欄位：

| 欄位 | 類型和預設值 | 說明 |
| --- | --- | --- |
| `dmp_path` | 字串，必填 | Step 1 要掃描的 DMP 檔案。相對路徑以配置檔所在目錄為基準解析。 |
| `output_path` | 字串，預設 `config.toml` | Step 1 寫出、Step 2 更新的輸出配置檔。 |
| `run_step2` | 布林值，預設 `true` | 設為 `false` 時只執行 Step 1。可以寫在頂層，也可以寫在 `[step2]`。 |
| `pak_paths` | 字串或字串陣列，預設空 | 精確 PAK 檔案或 glob 模式。例如 `"base.pak"`、`"*.pak"`、`["base.pak", "patch_001.pak"]`。 |
| `pak_dirs` | 字串或字串陣列，預設空 | 要用 `pak_glob` 掃描的目錄，也可以直接寫 glob 模式，例如 `"*.[pP][aA][kK]"`。 |
| `pak_dir` | 字串或字串陣列，預設空 | `pak_dirs` 的相容別名。新配置建議使用 `pak_dirs`。 |
| `pak_glob` | 字串，預設 `*.[pP][aA][kK]` | 僅用於 `pak_dirs` 中的目錄項，用來篩選目錄裡的 PAK 檔案。 |

`[step2]` 欄位：

| 欄位 | 類型和預設值 | 說明 |
| --- | --- | --- |
| `selected_extensions` | 字串、字串陣列或省略 | 要搜尋的副檔名。省略或寫 `"all_missing"` 表示搜尋 Step 1 中有未帶版本路徑證據的副檔名；`"missing"` 和 `"auto"` 是別名。寫 `"all"` 時會包含所有擁有 Step 1 路徑證據的副檔名。支援 CSV 字串，例如 `"tex,rcol"`，也支援陣列，例如 `["tex", "rcol"]`。副檔名前面的點可寫可不寫。 |
| `mode` | 字串，預設 `"small_range"` | 候選版本規劃模式。可選值：`"small_range"`、`"adaptive"`、`"custom"`、`"auto_detect"`。 |
| `min_version` | 整數，預設 `0` | 在 `small_range` 中表示起始版本號；在 `auto_detect` 的 numeric profile 中表示從預設最小值向下擴展多少。必須非負。 |
| `max_version` | 整數，預設 `4096` | 在 `small_range` 中表示結束版本號；在 `auto_detect` 的 numeric profile 中表示從預設最大值向上擴展多少。除 `auto_detect` 外必須大於等於 `min_version`。 |
| `custom_versions` | 字串，預設空 | 僅用於 `custom` 模式。支援逗號、換行和範圍，例如 `"12,18,30-40"`。 |
| `neighbor_radius` | 整數，預設 `32` | 僅用於 `adaptive` 模式，對已知版本號左右各擴展這個半徑。 |
| `date_start` | 字串，預設空 | 用於 `auto_detect` 的 `date_code` profile，並且 profile 裡存在 `priority_dates` 時生效。含義是 `Date -days`，即向最早優先日期之前擴展多少天。 |
| `date_end` | 字串，預設空 | 用於 `auto_detect` 的 `date_code` profile，並且 profile 裡存在 `priority_dates` 時生效。含義是 `Date +days`，即向最晚優先日期之後擴展多少天。設定為 `"today"` 時，會擴展到本機目前日期；如果目前日期早於預設裡的最晚優先日期，則不會縮小預設範圍。 |
| `processes` | 整數，預設 `0` | CPU worker 數量。`0` 表示使用本機 CPU 核心數。 |
| `include_platform_suffixes` | 布林值，預設 `true` | 根據路徑證據產生 `.STM`、`.X64` 等平台後綴變體。 |
| `language_mode` | 字串，預設 `"localized"` | 語言後綴模式。可選值：`"localized"`、`"off"`、`"all"`。 |
| `include_streaming` | 布林值，預設 `true` | 根據路徑證據產生 `streaming/` 路徑變體。 |
| `include_versioned_extensions` | 布林值，預設 `false` | 當 `selected_extensions` 省略或為 `"all_missing"` 時，也自動選擇 Step 1 中只有已帶版本路徑證據的副檔名。某個副檔名一旦被選中，Step 2 總會同時使用該副檔名的已帶版本和未帶版本 raw path 作為證據。 |
| `request_gpu` | 布林值，預設 `false` | 請求使用 torch CUDA 加速。如果沒有可用 CUDA 或 `torch`，會自動回退到 CPU 並在日誌裡說明原因。 |
| `gpu_batch_size` | 整數，預設 `16384` | 所有選中 CUDA 裝置的預設 GPU candidate batch size。目前不會自動調參。 |
| `gpu_devices` | `"auto"`、整數或整數陣列，預設 `[]` | 要使用的 CUDA 裝置。`"auto"` 或空值表示使用所有可見 CUDA 裝置。例如 `0`、`[0, 1, 2, 3]`、`"0,1"`。 |
| `gpu_workers_per_device` | 整數，預設 `1` | 每張 CUDA 裝置啟動幾個 worker 行程。建議先用 `1`，更高值可能增加資源競爭。 |
| `gpu_batch_sizes` | 字串或 TOML 表，預設空 | 可選的每卡 batch 覆蓋。字串寫法：`"0:524288,1:262144"`；TOML 表寫法：`{0 = 524288, 1 = 262144}`。值必須為正整數。 |

布林欄位建議直接使用 TOML 的 `true` / `false`，數字欄位必須寫成 TOML 整數。CLI 也能識別 `"yes"`、`"no"` 這類布林字串，但普通布林值更清楚。

## 基本流程

1. 選擇精確到檔案的 `.DMP`。
2. 選擇 `config.toml` 的儲存位置。
3. 加入一個或多個 `.pak` 檔案。
4. 執行 Step 1，掃描 DMP 並匯出已知後綴。
5. 如果 Step 1 提示存在缺失副檔名，選擇你想搜尋的副檔名。
6. 可選勾選 `Show versioned-only extensions`，列出只在 DMP 中以已帶版本形式出現的副檔名。
7. 執行 Step 2，發現版本後綴，並將成功匹配的結果合併回 `config.toml`。

Step 1 成功完成前，Step 2 會保持停用狀態。
Step 1 或 Step 2 執行時，檔案輸入和任務選項會被鎖定。Step 2 執行期間僅保留 `Stop` 可用。

## 輸出

選擇輸出位置後，工具會寫出：

- `config.toml`：供 ree-path-searcher / ree-pak-researcher 風格工具使用。
- `<name>.missing_versions.txt`：第一步中發現的、沒有數字版本後綴的 raw path 彙總。

## 後綴探索策略

Step 2 會從第一步的 raw path 證據中取出你選中的副檔名，產生候選路徑，例如：

```text
natives/STM/<raw_path>.<version>
natives/STM/<raw_path>.<version>.X64
natives/STM/<raw_path>.<version>.STM
...
```

隨後工具會計算每個候選路徑的 RE Engine mixed UTF-16 hash，並與從 PAK entry table 讀取到的 hash 集合進行匹配。匹配成功後，版本號會合併回 `suffix_map`，並重新儲存 `config.toml`。

候選生成會結合 profile 和 DMP 中的路徑證據。Step 1 會為每個 raw path 記錄它是否來自 `streaming/` 路徑，以及是否已經帶有 `.STM`、`.X64` 這類平台尾綴。對已選中的副檔名，Step 2 會同時使用已帶版本和未帶版本 raw path 作為證據，並利用這些資訊減少不必要的路徑變體。

## 候選模式

`Candidate mode` 只控制 Step 2 如何產生 `<raw_path>.<version>` 裡的候選版本號列表。路徑變體由 `Platform suffixes`、`Languages`、`Streaming variants` 分別控制。

- `small_range`：逐個嘗試 `Min version` 到 `Max version` 之間的所有版本號，包含兩端。預設範圍是 `0..4096`。這個模式涵蓋最廣，但耗時也可能最長。
- `adaptive`：根據 Step 1 已經從 DMP 中找到的同副檔名已知版本號，按 `Neighbor radius` 向左右擴展。預設半徑是 `32`，例如已知版本 `100` 會規劃 `68..132`。如果所選副檔名沒有任何已知版本，則退回 `Min version..Max version` 範圍。
- `custom`：只嘗試 `Custom versions` 中手動填寫的版本號。可以用逗號或換行分隔，也可以寫範圍，例如 `12, 18, 30-40`。程式會去重並排序。這個模式會忽略 `Min version`、`Max version` 和 `Neighbor radius`。
- `auto_detect`：從專案根目錄的 `file_suffix_profiles.json` 讀取預設，並按所選副檔名分別規劃版本號。`numeric` 類型把 `priority_versions` 當作基準範圍：`Min version` 從預設下界向下擴展，`Max version` 從預設上界向上擴展。例如預設是 `2..38`，`Min version = 10` 且 `Max version = 4096` 時，會搜尋 `0..4134`。`date_code` 類型把 `priority_dates` 當作基準日期範圍；`Date -days` 向前擴展日期下界，`Date +days` 向後擴展日期上界，`Date +days = today` 會使用本機目前日期作為上界，並優先嘗試 `priority_tails`，再嘗試剩餘的 `000..999` 尾號。

一般建議：同時搜尋多種不同檔案類型時，優先試 `auto_detect`；Step 1 已經找到相關已知版本時可試 `adaptive`；需要掃得更廣時用 `small_range`；已經知道可能版本號時用 `custom`，速度會更可控。

## 語言模式

`Languages` 控制 Step 2 是否產生 `.Ja` 和 `.En` 語言探針後綴。這些探針用於確認數字後綴是否存在，避免把所有 RE Engine 語言標籤都展開搜尋。

- `localized`：預設模式。只對看起來是本地化資源的路徑產生語言後綴：在 `file_suffix_profiles.json` 中標記了 `"language_search": true` 的副檔名、內建本地化副檔名（例如 `.msg`、`.asrc`、`.bnk`、`.pck`、`.sbnk`、`.spck`），或 raw path 中包含 `/message/`、`/text/`、`/subtitle/`、`/voice`、`/dialog/`、`/localization/` 等本地化目錄關鍵詞。
- `off`：完全不產生語言後綴變體。
- `all`：對所有選中的路徑都產生 `.Ja` 和 `.En` 探針後綴。

預設檔案使用普通 JSON，方便後續不改程式碼直接調整。頂層 `languages` 陣列會被產生的 TOML 配置檔直接復用，預設保留完整 RE Engine 語言列表，並且獨立於 Step 2 的 `.Ja` / `.En` 搜尋探針。它也提供基準範圍和優先值，最終範圍由 UI 決定擴展多少。在 `extensions` 下新增或修改副檔名即可：普通數字後綴使用 `suffix_type = "numeric"` 和可選 `priority_versions`，`YYMMDD` 加數字尾號的日期型後綴使用 `suffix_type = "date_code"` 和可選 `priority_dates`、`priority_tails`。只有常見 RE Engine 語言後綴的檔案類型才建議添加 `"language_search": true`。

## 候選剪枝

每個副檔名 profile 還可以控制路徑變體：

- `language_search`：`true` 為該副檔名啟用 `.Ja` 和 `.En` 語言探針後綴；`false` 即使在寬語言模式下也停用。
- `streaming_search`：`false` 停用 `streaming/` 變體，`true` 對每條路徑都搜尋 streaming，`"observed"` 只對 DMP 中確實見過 streaming 的路徑搜尋。
- `platform_search`：`false` 停用 `.X64` / `.STM` 變體，`"observed"` 只搜尋 DMP 中見過的平台尾綴，也可以寫成 `["STM"]` 這類清單來明確限制。

如果省略 `streaming_search`，Step 2 預設按路徑級 streaming 證據生成，而不是對所有 raw path 都翻倍。如果省略 `platform_search`，UI 的 `Platform suffixes` 會保持原本的寬搜尋行為。

## 效能說明

CPU 匹配會預先計算 `natives/STM/<raw_path>.` 這類長路徑前綴的 hash 狀態，再追加預編碼的版本號、平台和語言尾綴片段，避免每個候選都重新編碼和重新 hash 完整路徑。

同一個 PAK group 內搜尋多個副檔名時，CPU worker 會重用。PAK entry hash 會依路徑、檔案大小和修改時間快取在 workflow 中，因此 PAK 未變更時重複執行 Step 2 可以跳過中繼資料讀取。

## GPU Batch Size

啟用 `GPU acceleration (CUDA only)` 後，UI 會顯示 `GPU batch size`。預設值為 `16384`。

如果 GPU 使用率偏低且顯示記憶體占用較低，可以逐步增大：

```text
16384 -> 32768 -> 65536 -> 131072
```

如果出現 CUDA out of memory、系統明顯卡頓或不穩定，就降低一檔。更大的 batch 不一定總是更快，因為候選路徑產生和 UTF-16 編碼仍有 CPU 端開銷。

如果未安裝 `torch`、`torch` 不是 CUDA 版，或目前沒有可用 CUDA 裝置，程式會自動降級到 CPU 多行程，並在日誌中提示原因。

## 多 GPU 搜尋

啟用 GPU 模式後，`GPU devices = auto` 會使用所有可見 CUDA 裝置。也可以在 GUI 中填寫 `0,1`，或在 CLI 配置裡寫 `gpu_devices = [0, 1]` 來限制使用的顯示卡。

搜尋調度器會把版本號 chunk 動態分發給 GPU worker，速度更快的顯示卡完成後會繼續取得後續任務。匹配結果由主行程合併，已經發現的版本號會在 worker 之間共享，用來減少重複搜尋。

`gpu_batch_size` 是所有顯示卡的預設 batch size。如果不同顯示卡顯存不同，可以設定每卡覆蓋：

```toml
[step2]
request_gpu = true
gpu_devices = [0, 1]
gpu_batch_size = 262144
gpu_batch_sizes = "0:524288,1:131072"
gpu_workers_per_device = 1
```
