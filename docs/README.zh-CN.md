# RE File Hash Exporter

[English](../README.md) | [简体中文](README.zh-CN.md) | [繁體中文](README.zh-TW.md)

`RE File Hash Exporter` 是一个用于 RE Engine DMP / PAK 工作流的 `config.toml` 构建工具。

它会从指定的 `.DMP` 文件中扫描资源路径，导出已知的数字版本后缀，提示缺失版本号的路径，并可选择使用 PAK hash 证据发现缺失后缀。

## 功能

- 从一个精确指定的 `.DMP` 文件扫描 UTF-16LE 资源路径。
- 第一步导出 DMP 中已经带数字后缀的 `suffix_map`。
- 将 DMP 中只有 `name.ext`、没有 `.version` 的 raw path 按扩展名汇总并提示。
- 第二步允许手动选择扩展名，并在候选范围内发现版本号后缀。
- 提供 `auto_detect` 候选规划模式，可从根目录 `file_suffix_profiles.json` 读取可编辑预设。
- 第二步可选显示已经存在后缀的扩展名，用于继续搜索可能新增的版本后缀。
- 后缀发现只读取 PAK 元数据 hash，不解包 PAK 内容。
- 支持批量添加多个 PAK 文件。
- 当 PAK 文件未变化时，重复执行 Step 2 会复用已读取的 PAK 元数据缓存。
- CPU 模式使用多进程并行匹配。
- CPU 搜索会按 PAK 组复用 worker，并复用预计算的 UTF-16 hash 前缀状态。
- 勾选 `GPU acceleration (CUDA only)` 且安装 CUDA 可用的 `torch` 时，会使用 torch CUDA 加速。
- 选择或检测到多张 CUDA 显卡时，可并行使用多卡加速。
- GPU 搜索会批量处理预编码的 UTF-16 候选片段，减少完整路径字符串构造和重复编码。
- 只有启用 GPU 模式时才显示 GPU 专用设置。
- 后缀发现过程中可随时点击 `Stop` 停止搜索。
- 扫描或后缀发现运行时会锁定文件输入和任务选项，避免中途修改输入。

## 依赖

安装 `requirements.txt` 中列出的 Python 包：

```powershell
pip install -r requirements.txt
```

`torch` 仅在需要 GPU 加速时才是运行时必需项。若要使用 CUDA 加速，请安装与你本机 NVIDIA 驱动和 CUDA 环境匹配的 CUDA 版 PyTorch。

## 运行

```powershell
python main.py
```

## CLI 模式

CLI 模式会读取指定的 TOML 配置文件，不打开 GUI，直接顺序执行 Step 1 和 Step 2：

```powershell
python main.py --cli <config-file.toml>
# 或
python main.py cli <config-file.toml>
```

配置里的相对路径都以配置文件所在目录为基准解析。`output_path` 未填写时，默认写到同目录的 `config.toml`。
执行 Step 2 时，CLI 会使用 Rich 在终端底部固定显示进度条，上方继续滚动打印日志。
Step 2 运行时按一次 `Ctrl+C` 会请求优雅停止，已经找到的部分匹配会合并写回输出配置，行为等同 GUI 的 `Stop`；再次按 `Ctrl+C` 才会强制中断。

完整示例：

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

推荐结构：输入、输出和 PAK 选择写在顶层，后缀发现选项写在 `[step2]`。Step 2 选项也可以放在顶层；兼容旧配置的 `[bruteforce]` 表也会被读取，但同时存在时 `[step2]` 优先生效。

顶层字段：

| 字段 | 类型和默认值 | 说明 |
| --- | --- | --- |
| `dmp_path` | 字符串，必填 | Step 1 要扫描的 DMP 文件。相对路径以配置文件所在目录为基准解析。 |
| `output_path` | 字符串，默认 `config.toml` | Step 1 写出、Step 2 更新的输出配置文件。 |
| `run_step2` | 布尔值，默认 `true` | 设为 `false` 时只执行 Step 1。可以写在顶层，也可以写在 `[step2]`。 |
| `pak_paths` | 字符串或字符串数组，默认空 | 精确 PAK 文件或 glob 模式。例如 `"base.pak"`、`"*.pak"`、`["base.pak", "patch_001.pak"]`。 |
| `pak_dirs` | 字符串或字符串数组，默认空 | 要用 `pak_glob` 扫描的目录，也可以直接写 glob 模式，例如 `"*.[pP][aA][kK]"`。 |
| `pak_dir` | 字符串或字符串数组，默认空 | `pak_dirs` 的兼容别名。新配置建议使用 `pak_dirs`。 |
| `pak_glob` | 字符串，默认 `*.[pP][aA][kK]` | 仅用于 `pak_dirs` 中的目录项，用来筛选目录里的 PAK 文件。 |

`[step2]` 字段：

| 字段 | 类型和默认值 | 说明 |
| --- | --- | --- |
| `selected_extensions` | 字符串、字符串数组或省略 | 要搜索的扩展名。省略或写 `"all_missing"` 表示搜索 Step 1 中有未带版本路径证据的扩展名；`"missing"` 和 `"auto"` 是别名。写 `"all"` 时会包含所有拥有 Step 1 路径证据的扩展名。支持 CSV 字符串，例如 `"tex,rcol"`，也支持数组，例如 `["tex", "rcol"]`。扩展名前面的点可写可不写。 |
| `mode` | 字符串，默认 `"small_range"` | 候选版本规划模式。可选值：`"small_range"`、`"adaptive"`、`"custom"`、`"auto_detect"`。 |
| `min_version` | 整数，默认 `0` | 在 `small_range` 中表示起始版本号；在 `auto_detect` 的 numeric profile 中表示从预设最小值向下扩展多少。必须非负。 |
| `max_version` | 整数，默认 `4096` | 在 `small_range` 中表示结束版本号；在 `auto_detect` 的 numeric profile 中表示从预设最大值向上扩展多少。除 `auto_detect` 外必须大于等于 `min_version`。 |
| `custom_versions` | 字符串，默认空 | 仅用于 `custom` 模式。支持逗号、换行和范围，例如 `"12,18,30-40"`。 |
| `neighbor_radius` | 整数，默认 `32` | 仅用于 `adaptive` 模式，对已知版本号左右各扩展这个半径。 |
| `date_start` | 字符串，默认空 | 用于 `auto_detect` 的 `date_code` profile，并且 profile 里存在 `priority_dates` 时生效。含义是 `Date -days`，即向最早优先日期之前扩展多少天。 |
| `date_end` | 字符串，默认空 | 用于 `auto_detect` 的 `date_code` profile，并且 profile 里存在 `priority_dates` 时生效。含义是 `Date +days`，即向最晚优先日期之后扩展多少天。设置为 `"today"` 时，会扩展到本机当前日期；如果当前日期早于预设里的最晚优先日期，则不会缩小预设范围。 |
| `processes` | 整数，默认 `0` | CPU worker 数量。`0` 表示使用本机 CPU 核心数。 |
| `include_platform_suffixes` | 布尔值，默认 `true` | 根据路径证据生成 `.STM`、`.X64` 等平台后缀变体。 |
| `language_mode` | 字符串，默认 `"localized"` | 语言后缀模式。可选值：`"localized"`、`"off"`、`"all"`。 |
| `include_streaming` | 布尔值，默认 `true` | 根据路径证据生成 `streaming/` 路径变体。 |
| `include_versioned_extensions` | 布尔值，默认 `false` | 当 `selected_extensions` 省略或为 `"all_missing"` 时，也自动选择 Step 1 中只有已带版本路径证据的扩展名。某个扩展一旦被选中，Step 2 总会同时使用该扩展的已带版本和未带版本 raw path 作为证据。 |
| `request_gpu` | 布尔值，默认 `false` | 请求使用 torch CUDA 加速。如果没有可用 CUDA 或 `torch`，会自动回退到 CPU 并在日志里说明原因。 |
| `gpu_batch_size` | 整数，默认 `16384` | 所有选中 CUDA 设备的默认 GPU candidate batch size。当前不会自动调参。 |
| `gpu_devices` | `"auto"`、整数或整数数组，默认 `[]` | 要使用的 CUDA 设备。`"auto"` 或空值表示使用所有可见 CUDA 设备。例如 `0`、`[0, 1, 2, 3]`、`"0,1"`。 |
| `gpu_workers_per_device` | 整数，默认 `1` | 每张 CUDA 设备启动几个 worker 进程。建议先用 `1`，更高值可能增加资源竞争。 |
| `gpu_batch_sizes` | 字符串或 TOML 表，默认空 | 可选的每卡 batch 覆盖。字符串写法：`"0:524288,1:262144"`；TOML 表写法：`{0 = 524288, 1 = 262144}`。值必须为正整数。 |

布尔字段建议直接使用 TOML 的 `true` / `false`，数字字段必须写成 TOML 整数。CLI 也能识别 `"yes"`、`"no"` 这类布尔字符串，但普通布尔值更清楚。

## 基本流程

1. 选择精确到文件的 `.DMP`。
2. 选择 `config.toml` 的保存位置。
3. 添加一个或多个 `.pak` 文件。
4. 执行 Step 1，扫描 DMP 并导出已知后缀。
5. 如果 Step 1 提示存在缺失扩展名，选择你想搜索的扩展名。
6. 可选勾选 `Show versioned-only extensions`，列出只在 DMP 中以已带版本形式出现的扩展名。
7. 执行 Step 2，发现版本后缀，并将成功匹配的结果合并回 `config.toml`。

Step 1 成功完成前，Step 2 会保持禁用状态。
Step 1 或 Step 2 运行时，文件输入和任务选项会被锁定。Step 2 运行期间仅保留 `Stop` 可用。

## 输出

选择输出位置后，工具会写出：

- `config.toml`：供 ree-path-searcher / ree-pak-researcher 风格工具使用。
- `<name>.missing_versions.txt`：第一步中发现的、没有数字版本后缀的 raw path 汇总。

## 后缀发现策略

Step 2 会从第一步的 raw path 证据中取出你选中的扩展名，生成候选路径，例如：

```text
natives/STM/<raw_path>.<version>
natives/STM/<raw_path>.<version>.X64
natives/STM/<raw_path>.<version>.STM
...
```

随后工具会计算每个候选路径的 RE Engine mixed UTF-16 hash，并与从 PAK entry table 读取到的 hash 集合进行匹配。匹配成功后，版本号会合并回 `suffix_map`，并重新保存 `config.toml`。

候选生成会结合 profile 和 DMP 中的路径证据。Step 1 会为每个 raw path 记录它是否来自 `streaming/` 路径，以及是否已经带有 `.STM`、`.X64` 这类平台尾缀。对已选中的扩展名，Step 2 会同时使用已带版本和未带版本 raw path 作为证据，并利用这些信息减少不必要的路径变体。

## 候选模式

`Candidate mode` 只控制 Step 2 如何生成 `<raw_path>.<version>` 里的候选版本号列表。路径变体由 `Platform suffixes`、`Languages`、`Streaming variants` 分别控制。

- `small_range`：逐个尝试 `Min version` 到 `Max version` 之间的所有版本号，包含两端。默认范围是 `0..4096`。这个模式覆盖最广，但耗时也可能最长。
- `adaptive`：根据 Step 1 已经从 DMP 中找到的同扩展名已知版本号，按 `Neighbor radius` 向左右扩展。默认半径是 `32`，例如已知版本 `100` 会规划 `68..132`。如果所选扩展名没有任何已知版本，则退回 `Min version..Max version` 范围。
- `custom`：只尝试 `Custom versions` 中手动填写的版本号。可以用逗号或换行分隔，也可以写范围，例如 `12, 18, 30-40`。程序会去重并排序。这个模式会忽略 `Min version`、`Max version` 和 `Neighbor radius`。
- `auto_detect`：从项目根目录的 `file_suffix_profiles.json` 读取预设，并按所选扩展名分别规划版本号。`numeric` 类型把 `priority_versions` 当作基准范围：`Min version` 从预设下界向下扩展，`Max version` 从预设上界向上扩展。例如预设是 `2..38`，`Min version = 10` 且 `Max version = 4096` 时，会搜索 `0..4134`。`date_code` 类型把 `priority_dates` 当作基准日期范围；`Date -days` 向前扩展日期下界，`Date +days` 向后扩展日期上界，`Date +days = today` 会使用本机当前日期作为上界，并优先尝试 `priority_tails`，再尝试剩余的 `000..999` 尾号。

一般建议：同时搜索多种不同文件类型时，优先试 `auto_detect`；Step 1 已经找到相关已知版本时可试 `adaptive`；需要扫得更广时用 `small_range`；已经知道可能版本号时用 `custom`，速度会更可控。

## 语言模式

`Languages` 控制 Step 2 是否生成 `.Ja` 和 `.En` 语言探针后缀。这些探针用于确认数字后缀是否存在，避免把所有 RE Engine 语言标签都展开搜索。

- `localized`：默认模式。只对看起来是本地化资源的路径生成语言后缀：在 `file_suffix_profiles.json` 中标记了 `"language_search": true` 的扩展名、内置本地化扩展名（例如 `.msg`、`.asrc`、`.bnk`、`.pck`、`.sbnk`、`.spck`），或 raw path 中包含 `/message/`、`/text/`、`/subtitle/`、`/voice`、`/dialog/`、`/localization/` 等本地化目录关键词。
- `off`：完全不生成语言后缀变体。
- `all`：对所有选中的路径都生成 `.Ja` 和 `.En` 探针后缀。

预设文件使用普通 JSON，方便后续不改代码直接调整。顶层 `languages` 数组会被生成的 TOML 配置文件直接复用，默认保留完整 RE Engine 语言列表，并且独立于 Step 2 的 `.Ja` / `.En` 搜索探针。它也提供基准范围和优先值，最终范围由 UI 决定扩展多少。在 `extensions` 下新增或修改扩展名即可：普通数字后缀使用 `suffix_type = "numeric"` 和可选 `priority_versions`，`YYMMDD` 加数字尾号的日期型后缀使用 `suffix_type = "date_code"` 和可选 `priority_dates`、`priority_tails`。只有常见 RE Engine 语言后缀的文件类型才建议添加 `"language_search": true`。

## 候选剪枝

每个扩展名 profile 还可以控制路径变体：

- `language_search`：`true` 为该扩展名启用 `.Ja` 和 `.En` 语言探针后缀；`false` 即使在宽语言模式下也禁用。
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

## 多 GPU 搜索

启用 GPU 模式后，`GPU devices = auto` 会使用所有可见 CUDA 设备。也可以在 GUI 中填写 `0,1`，或在 CLI 配置里写 `gpu_devices = [0, 1]` 来限制使用的显卡。

搜索调度器会把版本号 chunk 动态分发给 GPU worker，速度更快的显卡完成后会继续拿到后续任务。匹配结果由主进程合并，已经发现的版本号会在 worker 之间共享，用来减少重复搜索。

`gpu_batch_size` 是所有显卡的默认 batch size。如果不同显卡显存不同，可以设置每卡覆盖：

```toml
[step2]
request_gpu = true
gpu_devices = [0, 1]
gpu_batch_size = 262144
gpu_batch_sizes = "0:524288,1:131072"
gpu_workers_per_device = 1
```
