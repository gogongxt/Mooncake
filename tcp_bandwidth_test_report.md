# Mooncake 传输带宽测试报告

## 1. 测试环境

### TCP 测试（跨集群）

| 项目 | 机器 A (Target) | 机器 B (Initiator) |
|------|----------------|-------------------|
| 主机名 | ml-b1-ser070.nmg03 | ml-h203e-ser140.nmg03 |
| IP (SSH) | 10.93.165.13:8067 | 10.93.153.50:8067 |
| GPU | NVIDIA H200 × 8 | NVIDIA H20-3e × 8 |
| 网络 | eth0 (100Gbps) + eth400g0-7 (400Gbps RDMA) | eth0 (100Gbps) + eth400g0-7 (400Gbps RDMA) |

两台机器的 400G RDMA 网卡处于不同子网（A: 10.129.152.0/25, B: 10.128.184.0/25），RDMA 不通。TCP 测试通过管理网 eth0 (100Gbps) 进行。

### RDMA 测试（同集群，400Gbps）

| 项目 | 机器 C (Target) | 机器 A (Initiator) |
|------|----------------|-------------------|
| 主机名 | ml-b1-ser161.nmg03 | ml-b1-ser070.nmg03 |
| IP (SSH) | 10.93.160.25:8067 | 10.93.165.13:8067 |
| GPU | NVIDIA H200 × 8 | NVIDIA H200 × 8 |
| RDMA NIC | mlx5_0-2, mlx5_5-9 (400Gbps) | mlx5_0-2, mlx5_5-9 (400Gbps) |

两台 H200 通过 400G RDMA 网卡直连。需 `MC_FORCE_HCA=true` 强制 RDMA transport，并通过 `MC_CUSTOM_TOPO_JSON` 排除管理网 mlx5_3，避免内存注册到错误的网卡。

---

## 2. 测试工具与命令

### 2.1 测试工具

使用 Mooncake 自带的 `transfer_engine_bench`，源码位于：
`mooncake-transfer-engine/example/transfer_engine_bench.cpp`

### 2.2 运行方式

采用 P2PHANDSHAKE 模式（点对点直连，无需 metadata server），Target 先启动等待连接，Initiator 发起传输。

**Target（机器 A）：**
```bash
MC_FORCE_TCP=true transfer_engine_bench \
    --mode=target \
    --auto_discovery \
    --metadata_server=P2PHANDSHAKE \
    --use_vram=false \
    --duration=15
```

**Initiator（机器 B）：**
```bash
MC_FORCE_TCP=true transfer_engine_bench \
    --mode=initiator \
    --auto_discovery \
    --metadata_server=P2PHANDSHAKE \
    --segment_id=<target输出的segment名> \
    --use_vram=false \
    --duration=10 \
    --block_size=<测试值> \
    --threads=<测试值> \
    --batch_size=<测试值>
```

### 2.3 参数说明

| 参数 | 含义 | 默认值 |
|------|------|--------|
| `MC_FORCE_TCP=true` | 环境变量，强制使用 TCP 传输，跳过 RDMA | false |
| `--mode` | 运行模式：`target`（接收端）或 `initiator`（发送端） | initiator |
| `--auto_discovery` | 启用自动发现，Target 启动后输出 RPC 端口和 segment 名 | false |
| `--metadata_server` | 元数据服务器地址，`P2PHANDSHAKE` 表示点对点直连 | — |
| `--segment_id` | Target 的 segment 标识（从 Target 输出中获取），格式为 `hostname:port` | — |
| `--use_vram` | 是否使用 GPU 显存，`false` 使用 CPU DRAM | true |
| `--duration` | 测试持续时间（秒） | 10 |
| `--block_size` | 每个 TransferRequest 的数据量（字节） | 65536 (64KB) |
| `--threads` | 并发提交线程数 | 12 |
| `--batch_size` | 每个线程每轮提交的请求数 | 128 |

### 2.4 block_size 与 TCP 64KB chunk 的区别

`block_size` 和 TCP 内部的 `kDefaultBufferSize`（64KB，硬编码）是**不同层级**的概念：

- **`block_size`**：一个 `TransferRequest` 的数据总量，用户可配
- **`kDefaultBufferSize`**（64KB）：TCP `writeBody` 每次网络发送的 chunk 大小，硬编码不可配

当 `block_size=256KB` 时，TCP 内部仍然按 64KB 串行发送 4 次：

```
TransferRequest (256KB)
  └── 一个 Slice (256KB)
        └── writeBody 递归：
              Chunk 0: 64KB → send → 等回调
              Chunk 1: 64KB → send → 等回调
              Chunk 2: 64KB → send → 等回调
              Chunk 3: 64KB → send → 等回调
```

增大 `block_size` 减少的是 **TransferRequest 级别的调度开销**（task 创建、batch 提交等），不是 TCP 内部的 chunk 开销。

---

## 3. 测试结果

### 3.1 不同 block_size（threads=4, batch_size=32）

| block_size | 每请求 chunk 数 | 吞吐量 | 相对基线 |
|-----------|----------------|--------|---------|
| 64KB | 1 | 0.63 GB/s | 1.0x |
| 256KB | 4 | 2.23 GB/s | 3.5x |
| 1MB | 16 | 2.29 GB/s | 3.6x |

### 3.2 不同并发度（block_size=256KB）

| threads | batch_size | 总并发请求数 | 吞吐量 |
|---------|-----------|------------|--------|
| 1 | 1 | 1 | 0.30 GB/s |
| 4 | 32 | 128 | 2.23 GB/s |
| 8 | 64 | 512 | 1.82 GB/s |
| 12 | 128 | 1536 | 2.07 GB/s |

### 3.3 连接池效果（block_size=256KB, threads=4, batch_size=32）

| 配置 | 吞吐量 |
|------|--------|
| 默认（无连接池） | 2.23 GB/s |
| `MC_TCP_ENABLE_CONNECTION_POOL=1` | 2.25 GB/s |

### 3.4 GPU VRAM 模式对比（`--use_vram=true`，默认，含 DtoH/HtoD memcpy）

**不同 block_size（threads=4, batch_size=32）：**

| block_size | 吞吐量 (DRAM) | 吞吐量 (VRAM) | VRAM/DRAM 比 | DtoH 开销 |
|-----------|--------------|--------------|-------------|----------|
| 64KB | 0.63 GB/s | 0.62 GB/s | 98% | 可忽略 |
| 256KB | 2.23 GB/s | 1.39 GB/s | 62% | 显著 |
| 1MB | 2.29 GB/s | 1.64 GB/s | 72% | 中等 |

**单线程（block_size=256KB, threads=1, batch_size=1）：**

| 模式 | 吞吐量 |
|------|--------|
| DRAM | 0.30 GB/s |
| VRAM | 0.27 GB/s |

### 3.5 H200-H200 同集群 TCP 测试（eth0 100Gbps，ml-b1-ser161 ↔ ml-b1-ser070）

| block_size | DRAM | VRAM | VRAM/DRAM |
|-----------|------|------|-----------|
| 64KB | 0.64 GB/s | 0.65 GB/s | 102% |
| 256KB | 2.29 GB/s | 1.81 GB/s | 79% |
| 1MB | 2.21 GB/s | 1.69 GB/s | 76% |

H200-H200 结果与 H200-H20 高度一致，验证了瓶颈在 TCP 处理链路而非对端 GPU 类型。

### 3.6 RDMA 测试（400Gbps，H200 ↔ H200，ml-b1-ser161 ↔ ml-b1-ser070）

**关键配置：** 需要 `MC_FORCE_HCA=true` 强制 RDMA transport，并通过 `MC_CUSTOM_TOPO_JSON` 排除管理网 mlx5_3，确保内存注册到 400G 网卡。RDMA 模式下默认使用 VRAM，DRAM 模式需要自定义拓扑文件。

**VRAM 模式（GPU-Direct RDMA）：**

| block_size | threads | batch_size | 吞吐量 |
|-----------|---------|-----------|--------|
| 64KB | 1 | 1 | 5.13 GB/s |
| 64KB | 4 | 32 | 42.22 GB/s |
| 256KB | 4 | 32 | 43.34 GB/s |
| 1MB | 4 | 32 | 42.63 GB/s |
| 256KB | 1 | 1 | 13.66 GB/s |

**DRAM 模式（CPU 内存，无 DtoH/HtoD）：**

| block_size | threads | batch_size | 吞吐量 |
|-----------|---------|-----------|--------|
| 64KB | 1 | 1 | 4.89 GB/s |
| 64KB | 4 | 32 | 122.19 GB/s |
| 256KB | 4 | 32 | 247.42 GB/s |
| 1MB | 4 | 32 | **284.40 GB/s** |
| 256KB | 1 | 1 | 13.04 GB/s |

**RDMA vs TCP 对比（256KB, threads=4, batch_size=32）：**

| 传输方式 | DRAM | VRAM | VRAM/DRAM |
|---------|------|------|-----------|
| TCP (100Gbps eth0) | 2.23 GB/s | 1.39 GB/s | 62% |
| RDMA (400Gbps) | 247.42 GB/s | 43.34 GB/s | **18%** |

RDMA DRAM 达到 284 GB/s（约 2.27 Tbps），接近 400Gbps × 8 网卡的聚合带宽。VRAM 模式受限于 PCIe 带宽（GPU ↔ NIC），峰值约 43 GB/s。DRAM 和 VRAM 的差距（284 vs 43 GB/s，6.6x）正是 GPU-Direct RDMA 的 PCIe 瓶颈体现。

---

## 4. 分析

### 4.1 与网络理论带宽的差距

eth0 为 100Gbps（≈11.2 GB/s），iperf3 多线程可跑到 90Gbps（≈10.8 GB/s）。TCP 测试最高仅 2.3 GB/s，约为理论带宽的 **20%**。

瓶颈不在网络带宽，而在 **CPU 处理链路**。

### 4.2 瓶颈分析

**iperf3 的数据路径（纯网络）：**
```
用户 buffer → kernel TCP stack → NIC DMA → 网络 → NIC DMA → kernel → 用户 buffer
```
内核使用 `sendmsg` + `TCP_CORK` 批量发送，零拷贝，最小化 syscall 次数。

**Mooncake TCP 的数据路径（每个 64KB chunk）：**
```
① new char[64KB]           — 堆内存分配
② 指针偏移（CPU 内存）或 cudaMemcpy（GPU 内存）
③ asio::async_write         — 用户态 → 内核态 → NIC
④ 等待完成回调              — io_context 单线程调度
⑤ delete[]                  — 释放内存
⑥ 递归 writeBody            — 处理下一个 64KB
```

**主要瓶颈：**

| 瓶颈 | 影响 | 说明 |
|------|------|------|
| 单线程 io_context | 高 | 所有 async 回调在一个线程上串行调度，高并发时成为瓶颈 |
| 串行 chunk pipeline | 高 | 每个 64KB 必须等 async_write 回调后才发下一个，无 double-buffering |
| 每 chunk 一次 syscall | 中 | 每个 64KB 一次 write() 系统调用，1MB = 16 次 syscall |
| 堆内存分配/释放 | 低 | 每 chunk 一次 new/delete，开销约 0.1us，相对较小 |

### 4.3 block_size 影响大的原因

block_size 从 64KB 增大到 256KB 时吞吐提升 3.5x，原因是减少了 **TransferRequest 级别的调度开销**：

- 每个请求需要：task 创建 → slice 分配 → batch 提交 → 回调处理 → task 完成判定
- 64KB block：传 1MB 需要 16 个请求的调度开销
- 256KB block：传 1MB 只需 4 个请求的调度开销
- 1MB block：传 1MB 只需 1 个请求

256KB 到 1MB 提升很小（2.23 → 2.29 GB/s），说明此时瓶颈已经转移到网络 I/O 本身。

### 4.4 并发度不是越高越好

| threads | 吞吐 | 说明 |
|---------|------|------|
| 1 | 0.30 GB/s | 纯串行，网络 round-trip 延迟主导 |
| 4 | 2.23 GB/s | 并发弥补延迟，吞吐最优 |
| 8 | 1.82 GB/s | 开始下降，io_context 调度压力增大 |
| 12 | 2.07 GB/s | 继续下降，回调竞争加剧 |

4 线程是当前配置下的最优点。更多线程导致 io_context 回调队列拥塞，反而降低吞吐。

### 4.5 连接池无效的原因

连接池（`MC_TCP_ENABLE_CONNECTION_POOL=1`）避免了每次传输的 TCP 三次握手开销，但在持续传输场景下，连接建立是一次性成本，摊薄后影响可忽略。连接池更适合**大量短连接**的场景（如频繁的小请求）。

### 4.6 DtoH/HtoD memcpy 的影响

VRAM 模式相比 DRAM 模式，每个 64KB chunk 多了 **2 次 `cudaMemcpy`**（发送端 DtoH + 接收端 HtoD）。

| block_size | DRAM 吞吐 | VRAM 吞吐 | 性能损失 | 原因 |
|-----------|----------|----------|---------|------|
| 64KB | 0.63 GB/s | 0.62 GB/s | ~2% | 64KB 的 cudaMemcpy 耗时（~3-5us）相对网络 I/O（~50us）很小 |
| 256KB | 2.23 GB/s | 1.39 GB/s | **38%** | 4 次 chunk 的 cudaMemcpy 累积，且与网络 I/O 串行无重叠 |
| 1MB | 2.29 GB/s | 1.64 GB/s | **28%** | 16 次 chunk 的 cudaMemcpy，但网络 I/O 占比更高 |

**关键发现：** block_size 越小，DtoH 的相对开销越大。64KB 时几乎无影响（网络延迟主导），256KB 时影响显著（cudaMemcpy 和网络 I/O 串行叠加）。

**为什么 VRAM 模式没有 double-buffering：** 当前代码中，每个 chunk 的流程是 `cudaMemcpy(DtoH) → async_write → 等回调 → delete`，cudaMemcpy 是同步阻塞的，无法与网络 I/O 重叠。如果改用 `cudaMemcpyAsync` + pinned memory + 双缓冲，DtoH 可以被网络发送隐藏，VRAM 和 DRAM 的差距会大幅缩小。

---

## 5. 结论

| 维度 | TCP (100Gbps eth0) | RDMA (400Gbps) |
|------|-------------------|----------------|
| DRAM 峰值吞吐 | 2.23 GB/s (18 Gbps) | **284.40 GB/s (2.27 Tbps)** |
| VRAM 峰值吞吐 | 1.64 GB/s (13 Gbps) | **43.34 GB/s (347 Gbps)** |
| VRAM/DRAM 比 | 62% (256KB) | **18%** (256KB) |
| 主要瓶颈 | io_context + 串行 chunk | PCIe 带宽 (GPU ↔ NIC) |
| 最优配置 | 256KB, 4t, 32b | 1MB, 4t, 32b |

**TCP 与 iperf3 的差距根因：** iperf3 使用内核零拷贝 + 批量 syscall，Mooncake TCP 使用用户态逐 chunk async_write + 回调调度，效率差距约 5x。

**RDMA DRAM vs VRAM 差距（284 vs 43 GB/s，6.6x）：** DRAM 模式下 NIC 直接通过 PCIe DMA 读写 CPU 内存，8 张 400G 网卡可聚合带宽。VRAM 模式下 GPU-Direct RDMA 的数据路径是 GPU → PCIe → NIC → 网络 → NIC → PCIe → GPU，单卡 PCIe Gen5 x16 理论带宽约 64 GB/s，实际约 43 GB/s，成为瓶颈。

**RDMA VRAM 吞吐稳定在 ~43 GB/s：** 无论 block_size 从 64KB 到 1MB，VRAM 吞吐都稳定在 42-43 GB/s，说明瓶颈完全在 PCIe 链路而非网络或 RDMA 协议开销。

**TCP VRAM 开销更大（38% vs RDMA 的 82%）：** TCP 256KB 时 VRAM 比 DRAM 慢 38%，RDMA 256KB 时 VRAM 比 DRAM 慢 82%（43 vs 247 GB/s）。但 RDMA 的绝对吞吐仍然高 20 倍以上。

---

## 6. 实操指南与踩坑记录

### 6.1 TCP 测试（简单，无特殊要求）

TCP 测试不需要特殊配置，直接用 `MC_FORCE_TCP=true` 即可：

```bash
# Target（机器 A）
MC_FORCE_TCP=true transfer_engine_bench --mode=target --auto_discovery --metadata_server=P2PHANDSHAKE --use_vram=false --duration=60

# Initiator（机器 B，segment_id 从 target 输出获取）
MC_FORCE_TCP=true transfer_engine_bench --mode=initiator --auto_discovery --metadata_server=P2PHANDSHAKE --segment_id=<hostname:port> --use_vram=false --duration=10 --block_size=262144 --threads=4 --batch_size=32
```

### 6.2 RDMA 测试（需要注意网卡选择）

RDMA 测试踩过的坑：

**坑 1：默认不装 RDMA transport。** 不设置 `MC_FORCE_HCA=true` 时，auto-discovery 可能只安装 TCP transport，导致 `Transport tcp not installed` 错误。

**坑 2：内存注册到管理网。** 即使装了 RDMA transport，内存注册可能选到管理网 mlx5_3（走 eth0），而不是 400G 网卡。表现为 `transport retry counter exceeded` 错误——RDMA 连接建立在管理网上，无法到达对端的 400G NIC。

**坑 3：RDMA VRAM 模式默认走 GPU 0 → mlx5_0。** DRAM 模式回退到 CPU NUMA 拓扑，可能选到 mlx5_3。需要自定义拓扑文件排除管理网。

**排查步骤：**

```bash
# 1. 先确认实际的 GPU ↔ IB 网卡拓扑
python3 /nfs/ofs-llm-ssd/user/gogongxt/luban_scripts/get_ib_devices.py

# 输出示例：
# GPU 0000:ba:00.0 ↔ IB mlx5_8
# GPU 0000:3a:00.0 ↔ IB mlx5_2
# ...
# IB devices (GPU-attached): mlx5_0,mlx5_1,mlx5_2,mlx5_5,mlx5_6,mlx5_7,mlx5_8,mlx5_9
# 注意：mlx5_3 不在列表中（它是管理网 eth0 的 RDMA 设备）

# 2. 确认哪些网卡是 400G（GPU 关联的）
#    管理网一般是 mlx5_3（对应 eth0），需要排除

# 3. 检查各网卡 IP 子网
for iface in eth400g0 eth400g1 eth400g2 eth400g3 eth400g4 eth400g5 eth400g6 eth400g7; do
    ip addr show $iface 2>/dev/null | grep 'inet ' | awk "{print \"$iface:\", \$2}"
done
```

**自定义拓扑文件（排除管理网 mlx5_3）：**

```json
{
  "cpu:0": [["mlx5_0","mlx5_1","mlx5_2","mlx5_5"],["mlx5_6","mlx5_7","mlx5_8","mlx5_9"]],
  "cpu:1": [["mlx5_6","mlx5_7","mlx5_8","mlx5_9"],["mlx5_0","mlx5_1","mlx5_2","mlx5_5"]],
  "cuda:0": [["mlx5_0"],["mlx5_1","mlx5_2","mlx5_5","mlx5_6","mlx5_7","mlx5_8","mlx5_9"]],
  "cuda:1": [["mlx5_1"],["mlx5_0","mlx5_2","mlx5_5","mlx5_6","mlx5_7","mlx5_8","mlx5_9"]],
  "cuda:2": [["mlx5_2"],["mlx5_0","mlx5_1","mlx5_5","mlx5_6","mlx5_7","mlx5_8","mlx5_9"]],
  "cuda:3": [["mlx5_5"],["mlx5_0","mlx5_1","mlx5_2","mlx5_6","mlx5_7","mlx5_8","mlx5_9"]],
  "cuda:4": [["mlx5_6"],["mlx5_0","mlx5_1","mlx5_2","mlx5_5","mlx5_7","mlx5_8","mlx5_9"]],
  "cuda:5": [["mlx5_7"],["mlx5_0","mlx5_1","mlx5_2","mlx5_5","mlx5_6","mlx5_8","mlx5_9"]],
  "cuda:6": [["mlx5_8"],["mlx5_0","mlx5_1","mlx5_2","mlx5_5","mlx5_6","mlx5_7","mlx5_9"]],
  "cuda:7": [["mlx5_9"],["mlx5_0","mlx5_1","mlx5_2","mlx5_5","mlx5_6","mlx5_7","mlx5_8"]]
}
```

保存为 `/tmp/topo.json`，两台机器都需要这个文件。

**完整 RDMA 测试命令：**

```bash
# === VRAM 模式（GPU-Direct RDMA，最简单） ===
# VRAM 模式下 GPU 0 自动映射到 mlx5_0（根据拓扑），不需要自定义 topo

# Target（机器 A）
MC_FORCE_HCA=true transfer_engine_bench --mode=target --auto_discovery --metadata_server=P2PHANDSHAKE --duration=60

# Initiator（机器 B）
MC_FORCE_HCA=true transfer_engine_bench --mode=initiator --auto_discovery --metadata_server=P2PHANDSHAKE --segment_id=<hostname:port> --duration=10 --block_size=262144 --threads=4 --batch_size=32

# === DRAM 模式（需要自定义拓扑排除管理网） ===

# Target（机器 A）
MC_FORCE_HCA=true MC_CUSTOM_TOPO_JSON=/tmp/topo.json transfer_engine_bench --mode=target --auto_discovery --metadata_server=P2PHANDSHAKE --use_vram=false --duration=60

# Initiator（机器 B）
MC_FORCE_HCA=true MC_CUSTOM_TOPO_JSON=/tmp/topo.json transfer_engine_bench --mode=initiator --auto_discovery --metadata_server=P2PHANDSHAKE --segment_id=<hostname:port> --use_vram=false --duration=10 --block_size=262144 --threads=4 --batch_size=32
```

### 6.3 环境变量速查

| 变量 | 作用 | TCP 需要 | RDMA 需要 |
|------|------|---------|----------|
| `MC_FORCE_TCP=true` | 强制 TCP transport | 是 | 否 |
| `MC_FORCE_HCA=true` | 强制 RDMA transport | 否 | **是** |
| `MC_CUSTOM_TOPO_JSON` | 自定义 GPU↔HCA 拓扑 | 否 | DRAM 模式需要 |
| `MC_MS_FILTERS` | 过滤可用 RDMA 设备 | 否 | 可选 |
