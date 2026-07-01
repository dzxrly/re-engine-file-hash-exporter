# RE File Hash Exporter

[English](../README.md) | [简体中文](README.zh-CN.md) | [繁體中文](README.zh-TW.md)

`RE File Hash Exporter` 是一个用于 RE Engine DMP / PAK 工作流的 `config.toml` 构建工具。

它会从指定的 `.DMP` 文件中扫描资源路径，导出已知的数字版本后缀，提示缺失版本号的路径，并可选择对缺失后缀进行 PAK hash 暴力匹配。

## 功能

- 从一个精确指定的 `.DMP` 文件扫描 UTF-16LE 资源路径。
- 第一步导出 DMP 中已经带数字后缀的 `suffix_map`。
- 将 DMP 中只有 `name.ext`、没有 `.version` 的 raw path 按扩展名汇总并提示。
- 第二步允许手动选择缺失扩展名，并暴力搜索候选版本号后缀。
- 提供 `auto_detect` 候选规划模式，可从根目录 `file_suffix_profiles.json` 读取可编辑预设。
- 第二步可选显示已经存在后缀的扩展名，用于继续搜索可能新增的版本后缀。
- 暴力匹配只读取 PAK 元数据 hash，不解包 PAK 内容。
- 支持批量添加多个 PAK 文件。
- 当 PAK 文件未变化时，重复执行 Step 2 会复用已读取的 PAK 元数据缓存。
- CPU 模式使用多进程并行匹配。
- CPU 搜索会按 PAK 组复用 worker，并复用预计算的 UTF-16 hash 前缀状态。
- 勾选 `GPU acceleration (CUDA only)` 且安装 CUDA 可用的 `torch` 时，会使用 torch CUDA 加速。
- GPU 搜索会批量处理预编码的 UTF-16 候选片段，减少完整路径字符串构造和重复编码。
- 只有启用 GPU 模式时才显示 `GPU batch size` 设置。
- 暴力匹配过程中可随时点击 `Stop` 停止搜索。
- 扫描或暴力匹配运行时会锁定文件输入和任务选项，避免中途修改输入。

## 依赖

安装 `requirements.txt` 中列出的 Python 包：

```powershell
pip install -r requirements.txt
```

`torch` 仅在需要 GPU 加速时才是运行时必需项。若要使用 CUDA 加速，请安装与你本机 NVIDIA 驱动和 CUDA 环境匹配的 CUDA 版 PyTorch。

## 运行

```powershell
cd C:\Software\mhws\re-file-hash-exporter
python main.py
```

## 基本流程

1. 选择精确到文件的 `.DMP`。
2. 选择 `config.toml` 的保存位置。
3. 添加一个或多个 `.pak` 文件。
4. 执行 Step 1，扫描 DMP 并导出已知后缀。
5. 如果 Step 1 提示存在缺失扩展名，选择你想搜索的扩展名。
6. 可选勾选 `Show versioned extensions`，对已经有已知后缀的扩展名继续搜索可能新增的版本后缀。
7. 执行 Step 2，暴力匹配版本后缀，并将成功匹配的结果合并回 `config.toml`。

Step 1 成功完成前，Step 2 会保持禁用状态。
Step 1 或 Step 2 运行时，文件输入和任务选项会被锁定。Step 2 运行期间仅保留 `Stop` 可用。

## 输出

选择输出位置后，工具会写出：

- `config.toml`：供 ree-path-searcher / ree-pak-researcher 风格工具使用。
- `<name>.missing_versions.txt`：第一步中发现的、没有数字版本后缀的 raw path 汇总。

## 暴力匹配策略

Step 2 会从第一步的 missing raw paths 中取出你选中的扩展名，生成候选路径，例如：

```text
natives/STM/<raw_path>.<version>
natives/STM/<raw_path>.<version>.X64
natives/STM/<raw_path>.<version>.STM
...
```

随后工具会计算每个候选路径的 RE Engine mixed UTF-16 hash，并与从 PAK entry table 读取到的 hash 集合进行匹配。匹配成功后，版本号会合并回 `suffix_map`，并重新保存 `config.toml`。

候选生成会结合 profile 和 DMP 中的路径证据。Step 1 会为每个 raw path 记录它是否来自 `streaming/` 路径，以及是否已经带有 `.STM`、`.X64` 这类平台尾缀。Step 2 会利用这些信息减少不必要的路径变体。

## 候选模式

`Candidate mode` 只控制 Step 2 如何生成 `<raw_path>.<version>` 里的候选版本号列表。路径变体由 `Platform suffixes`、`Languages`、`Streaming variants` 分别控制。

- `small_range`：逐个尝试 `Min version` 到 `Max version` 之间的所有版本号，包含两端。默认范围是 `0..4096`。这个模式覆盖最广，但耗时也可能最长。
- `adaptive`：根据 Step 1 已经从 DMP 中找到的同扩展名已知版本号，按 `Neighbor radius` 向左右扩展。默认半径是 `32`，例如已知版本 `100` 会规划 `68..132`。如果所选扩展名没有任何已知版本，则退回 `Min version..Max version` 范围。
- `custom`：只尝试 `Custom versions` 中手动填写的版本号。可以用逗号或换行分隔，也可以写范围，例如 `12, 18, 30-40`。程序会去重并排序。这个模式会忽略 `Min version`、`Max version` 和 `Neighbor radius`。
- `auto_detect`：从项目根目录的 `file_suffix_profiles.json` 读取预设，并按所选扩展名分别规划版本号。`numeric` 类型把 `priority_versions` 当作基准范围：`Min version` 从预设下界向下扩展，`Max version` 从预设上界向上扩展。例如预设是 `2..38`，`Min version = 10` 且 `Max version = 4096` 时，会搜索 `0..4134`。`date_code` 类型把 `priority_dates` 当作基准日期范围；`Date -days` 向前扩展日期下界，`Date +days` 向后扩展日期上界，并优先尝试 `priority_tails`，再尝试剩余的 `000..999` 尾号。

一般建议：同时搜索多种不同文件类型时，优先试 `auto_detect`；Step 1 已经找到相关已知版本时可试 `adaptive`；需要扫得更广时用 `small_range`；已经知道可能版本号时用 `custom`，速度会更可控。

## 语言模式

`Languages` 控制 Step 2 是否生成 `.Ja`、`.En`、`.ZhCN` 等语言后缀变体。

- `localized`：默认模式。只对看起来是本地化资源的路径生成语言后缀：在 `file_suffix_profiles.json` 中标记了 `"language_search": true` 的扩展名、内置本地化扩展名（例如 `.msg`、`.asrc`、`.bnk`、`.pck`、`.sbnk`、`.spck`），或 raw path 中包含 `/message/`、`/text/`、`/subtitle/`、`/voice`、`/dialog/`、`/localization/` 等本地化目录关键词。
- `off`：完全不生成语言后缀变体。
- `all`：对所有选中的路径都生成语言后缀变体，等同于旧版的宽搜索行为。

预设文件使用普通 JSON，方便后续不改代码直接调整。它提供基准范围和优先值，最终范围由 UI 决定扩展多少。在 `extensions` 下新增或修改扩展名即可：普通数字后缀使用 `suffix_type = "numeric"` 和可选 `priority_versions`，`YYMMDD` 加数字尾号的日期型后缀使用 `suffix_type = "date_code"` 和可选 `priority_dates`、`priority_tails`。只有常见 RE Engine 语言后缀的文件类型才建议添加 `"language_search": true`。

## 候选剪枝

每个扩展名 profile 还可以控制路径变体：

- `language_search`：`true` 为该扩展名启用语言尾缀；`false` 即使在宽语言模式下也禁用。
- `streaming_search`：`false` 禁用 `streaming/` 变体，`true` 对每条路径都搜索 streaming，`"observed"` 只对 DMP 中确实见过 streaming 的路径搜索。
- `platform_search`：`false` 禁用 `.X64` / `.STM` 变体，`"observed"` 只搜索 DMP 中见过的平台尾缀，也可以写成 `["STM"]` 这类列表来显式限制。

如果省略 `streaming_search`，Step 2 默认按路径级 streaming 证据生成，而不是对所有 raw path 都翻倍。如果省略 `platform_search`，UI 的 `Platform suffixes` 会保持原来的宽搜索行为。

## 性能说明

CPU 匹配会预计算 `natives/STM/<raw_path>.` 这类长路径前缀的 hash 状态，再追加预编码的版本号、平台和语言尾缀片段，避免每个候选都重新编码和重新 hash 完整路径。

同一个 PAK group 内搜索多个扩展名时，CPU worker 会复用。PAK entry hash 会按路径、文件大小和修改时间缓存在 workflow 中，因此 PAK 未变化时重复执行 Step 2 可以跳过元数据读取。

## GPU Batch Size

启用 `GPU acceleration (CUDA only)` 后，UI 会显示 `GPU batch size`。默认值为 `16384`。

如果 GPU 利用率偏低且显存占用较低，可以逐步增大：

```text
16384 -> 32768 -> 65536 -> 131072
```

如果出现 CUDA out of memory、系统明显卡顿或不稳定，就降低一档。更大的 batch 不一定总是更快，因为候选路径生成和 UTF-16 编码仍有 CPU 侧开销。

如果未安装 `torch`、`torch` 不是 CUDA 版，或当前没有可用 CUDA 设备，程序会自动降级到 CPU 多进程，并在日志中提示原因。
