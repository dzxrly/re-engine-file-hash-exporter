# RE File Hash Exporter

[English](../README.md) | [简体中文](README.zh-CN.md) | [繁體中文](README.zh-TW.md)

`RE File Hash Exporter` 是一个用于 RE Engine DMP / PAK 工作流的 `config.toml` 构建工具。

它会从指定的 `.DMP` 文件中扫描资源路径，导出已知的数字版本后缀，提示缺失版本号的路径，并可选择对缺失后缀进行 PAK hash 暴力匹配。

## 功能

- 从一个精确指定的 `.DMP` 文件扫描 UTF-16LE 资源路径。
- 第一步导出 DMP 中已经带数字后缀的 `suffix_map`。
- 将 DMP 中只有 `name.ext`、没有 `.version` 的 raw path 按扩展名汇总并提示。
- 第二步允许手动选择缺失扩展名，并暴力搜索候选版本号后缀。
- 第二步可选显示已经存在后缀的扩展名，用于继续搜索可能新增的版本后缀。
- 暴力匹配只读取 PAK 元数据 hash，不解包 PAK 内容。
- 支持批量添加多个 PAK 文件。
- CPU 模式使用多进程并行匹配。
- 勾选 `GPU acceleration (CUDA only)` 且安装 CUDA 可用的 `torch` 时，会使用 torch CUDA 加速。
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

## GPU Batch Size

启用 `GPU acceleration (CUDA only)` 后，UI 会显示 `GPU batch size`。默认值为 `16384`。

如果 GPU 利用率偏低且显存占用较低，可以逐步增大：

```text
16384 -> 32768 -> 65536 -> 131072
```

如果出现 CUDA out of memory、系统明显卡顿或不稳定，就降低一档。更大的 batch 不一定总是更快，因为候选路径生成和 UTF-16 编码仍有 CPU 侧开销。

如果未安装 `torch`、`torch` 不是 CUDA 版，或当前没有可用 CUDA 设备，程序会自动降级到 CPU 多进程，并在日志中提示原因。
