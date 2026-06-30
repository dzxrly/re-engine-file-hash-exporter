# RE File Hash Exporter

[English](../README.md) | [简体中文](README.zh-CN.md) | [繁體中文](README.zh-TW.md)

`RE File Hash Exporter` 是一個用於 RE Engine DMP / PAK 工作流程的 `config.toml` 建構工具。

它會從指定的 `.DMP` 檔案中掃描資源路徑，匯出已知的數字版本後綴，提示缺少版本號的路徑，並可選擇對缺失後綴進行 PAK hash 暴力匹配。

## 功能

- 從一個精確指定的 `.DMP` 檔案掃描 UTF-16LE 資源路徑。
- 第一步匯出 DMP 中已經帶有數字後綴的 `suffix_map`。
- 將 DMP 中只有 `name.ext`、沒有 `.version` 的 raw path 按副檔名彙總並提示。
- 第二步允許手動選擇缺失副檔名，並暴力搜尋候選版本號後綴。
- 提供 `auto_detect` 候選規劃模式，可從根目錄 `file_suffix_profiles.json` 讀取可編輯預設。
- 第二步可選顯示已經存在後綴的副檔名，用於繼續搜尋可能新增的版本後綴。
- 暴力匹配只讀取 PAK 中繼資料 hash，不解包 PAK 內容。
- 支援批次加入多個 PAK 檔案。
- CPU 模式使用多行程平行匹配。
- 勾選 `GPU acceleration (CUDA only)` 且安裝 CUDA 可用的 `torch` 時，會使用 torch CUDA 加速。
- 只有啟用 GPU 模式時才顯示 `GPU batch size` 設定。
- 暴力匹配過程中可隨時點擊 `Stop` 停止搜尋。
- 掃描或暴力匹配執行時會鎖定檔案輸入和任務選項，避免中途修改輸入。

## 依賴

安裝 `requirements.txt` 中列出的 Python 套件：

```powershell
pip install -r requirements.txt
```

`torch` 只有在需要 GPU 加速時才是執行階段必需項。若要使用 CUDA 加速，請安裝與本機 NVIDIA 驅動和 CUDA 環境相符的 CUDA 版 PyTorch。

## 執行

```powershell
cd C:\Software\mhws\re-file-hash-exporter
python main.py
```

## 基本流程

1. 選擇精確到檔案的 `.DMP`。
2. 選擇 `config.toml` 的儲存位置。
3. 加入一個或多個 `.pak` 檔案。
4. 執行 Step 1，掃描 DMP 並匯出已知後綴。
5. 如果 Step 1 提示存在缺失副檔名，選擇你想搜尋的副檔名。
6. 可選勾選 `Show versioned extensions`，對已經有已知後綴的副檔名繼續搜尋可能新增的版本後綴。
7. 執行 Step 2，暴力匹配版本後綴，並將成功匹配的結果合併回 `config.toml`。

Step 1 成功完成前，Step 2 會保持停用狀態。
Step 1 或 Step 2 執行時，檔案輸入和任務選項會被鎖定。Step 2 執行期間僅保留 `Stop` 可用。

## 輸出

選擇輸出位置後，工具會寫出：

- `config.toml`：供 ree-path-searcher / ree-pak-researcher 風格工具使用。
- `<name>.missing_versions.txt`：第一步中發現的、沒有數字版本後綴的 raw path 彙總。

## 暴力匹配策略

Step 2 會從第一步的 missing raw paths 中取出你選中的副檔名，產生候選路徑，例如：

```text
natives/STM/<raw_path>.<version>
natives/STM/<raw_path>.<version>.X64
natives/STM/<raw_path>.<version>.STM
...
```

隨後工具會計算每個候選路徑的 RE Engine mixed UTF-16 hash，並與從 PAK entry table 讀取到的 hash 集合進行匹配。匹配成功後，版本號會合併回 `suffix_map`，並重新儲存 `config.toml`。

## 候選模式

`Candidate mode` 只控制 Step 2 如何產生 `<raw_path>.<version>` 裡的候選版本號列表。平台後綴、語言後綴、`streaming/` 路徑等路徑變體仍由旁邊的核取方塊控制。

- `small_range`：逐個嘗試 `Min version` 到 `Max version` 之間的所有版本號，包含兩端。預設範圍是 `0..4096`。這個模式涵蓋最廣，但耗時也可能最長。
- `adaptive`：根據 Step 1 已經從 DMP 中找到的同副檔名已知版本號，按 `Neighbor radius` 向左右擴展。預設半徑是 `32`，例如已知版本 `100` 會規劃 `68..132`。如果所選副檔名沒有任何已知版本，則退回 `Min version..Max version` 範圍。
- `custom`：只嘗試 `Custom versions` 中手動填寫的版本號。可以用逗號或換行分隔，也可以寫範圍，例如 `12, 18, 30-40`。程式會去重並排序。這個模式會忽略 `Min version`、`Max version` 和 `Neighbor radius`。
- `auto_detect`：從專案根目錄的 `file_suffix_profiles.json` 讀取預設，並按所選副檔名分別規劃版本號。`numeric` 類型仍涵蓋完整的 `Min version..Max version` 範圍，但範圍內的 `priority_versions` 會優先嘗試。`date_code` 類型使用 `Date from` / `Date to`，先嘗試 `priority_tails`，再嘗試所選日期下剩餘的 `000..999` 尾號。

一般建議：同時搜尋多種不同檔案類型時，優先試 `auto_detect`；Step 1 已經找到相關已知版本時可試 `adaptive`；需要掃得更廣時用 `small_range`；已經知道可能版本號時用 `custom`，速度會更可控。

預設檔案使用普通 JSON，方便後續不改程式碼直接調整。它不會鎖定搜尋範圍，只改變候選順序；真正的範圍仍由 UI 控制。在 `extensions` 下新增或修改副檔名即可：普通數字後綴使用 `suffix_type = "numeric"` 和可選 `priority_versions`，`YYMMDD` 加數字尾號的日期型後綴使用 `suffix_type = "date_code"` 和可選 `priority_tails`。

## GPU Batch Size

啟用 `GPU acceleration (CUDA only)` 後，UI 會顯示 `GPU batch size`。預設值為 `16384`。

如果 GPU 使用率偏低且顯示記憶體占用較低，可以逐步增大：

```text
16384 -> 32768 -> 65536 -> 131072
```

如果出現 CUDA out of memory、系統明顯卡頓或不穩定，就降低一檔。更大的 batch 不一定總是更快，因為候選路徑產生和 UTF-16 編碼仍有 CPU 端開銷。

如果未安裝 `torch`、`torch` 不是 CUDA 版，或目前沒有可用 CUDA 裝置，程式會自動降級到 CPU 多行程，並在日誌中提示原因。
