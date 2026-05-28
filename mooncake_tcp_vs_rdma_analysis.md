# Mooncake Transfer Engine: TCP vs RDMA 数据路径分析

## 1. TCP (`MC_FORCE_TCP=true`) 数据路径

### 核心结论：有 DtoH / HtoD 内存拷贝

TCP 传输 GPU 内存时，数据路径为：

```
GPU ──[cudaMemcpy/DtoH]──> CPU staging buffer ──[TCP NIC]──> 远端CPU buffer ──[cudaMemcpy/HtoD]──> GPU
```

源码位置：`mooncake-transfer-engine/src/transport/tcp_transport/tcp_transport.cpp`

**发送端 `writeBody()`：**
```cpp
if (isCudaMemory(addr)) {
    dram_buffer = new char[buffer_size];       // 分配 CPU 堆内存
    cudaMemcpy(dram_buffer, addr + offset,     // DtoH: GPU → CPU
               buffer_size, cudaMemcpyDefault);
}
asio::async_write(socket_, asio::buffer(dram_buffer, buffer_size), ...);
// 回调中 delete[] dram_buffer
```

**接收端 `readBody()`：**
```cpp
if (isCudaMemory(addr)) {
    dram_buffer = new char[buffer_size];
}
asio::async_read(socket_, asio::buffer(dram_buffer, buffer_size), ...);
// 回调中:
cudaMemcpy(addr + offset, dram_buffer,         // HtoD: CPU → GPU
           transferred_bytes, cudaMemcpyDefault);
delete[] dram_buffer;
```

每个 64KB 的 chunk 都会经过一次 CPU staging，使用普通堆内存（`new char[]`），不是 pinned memory。

---

## 2. RDMA 数据路径

### 核心结论：无 DtoH，走 GPU-Direct RDMA，CPU 内存完全不参与

RDMA 传输 GPU 内存时，数据路径为：

```
GPU ──[PCIe, DMA-BUF/peermem]──> RDMA NIC ──[网络]──> 远端NIC ──[PCIe]──> GPU
```

源码位置：`mooncake-transfer-engine/src/transport/rdma_transport/rdma_context.cpp`

**内存注册阶段**——GPU 显存注册到 IB verbs，有两条路径：

路径 A：通过 DMA-BUF（默认，无需特殊内核模块）：
```cpp
// 获取 CUDA 分配的真实基地址（处理 PyTorch caching allocator 偏移）
cuMemGetAddressRange(&allocBase, &allocSize, (CUdeviceptr)addr, ...);
// 获取 GPU 内存的 DMA-BUF fd
cuMemGetHandleForAddressRange(&dmabuf_fd, allocBase, allocSize,
                              CU_MEM_RANGE_HANDLE_TYPE_DMA_BUF_FD, 0);
// 计算偏移并注册到 RDMA NIC
dmabuf_offset = (uintptr_t)addr - (uintptr_t)allocBase;
mrMeta.mr = ibv_reg_dmabuf_mr(pd_, dmabuf_offset, length, (uintptr_t)addr, dmabuf_fd, access);
```

路径 B：通过 nvidia-peermem 内核模块（需已加载，设置 `MC_WITH_NVIDIA_PEERMEM=1`）：
```cpp
// 直接注册 GPU 显存到 IB verbs，跳过 DMA-BUF
mrMeta.mr = ibv_reg_mr(pd_, addr, length, access);
```

两条路径在传输阶段行为一致——NIC 都直接通过 PCIe DMA 访问 GPU 显存。

**传输阶段**——NIC 直接操作已注册的内存地址，无任何 `cudaMemcpy`：
```cpp
sge.addr = (uint64_t)slice->source_addr;  // 直接指向 GPU 显存
sge.length = slice->length;
sge.lkey = slice->rdma.source_lkey;       // 来自 ibv_reg_dmabuf_mr
// ibv_post_send → NIC 直接通过 PCIe DMA 读写 GPU 显存
```

---

## 3. 对比总结

| 维度 | TCP | RDMA |
|------|-----|------|
| DtoH memcpy | 有，每 64KB 一次 | 无 |
| HtoD memcpy | 有，每 64KB 一次 | 无 |
| CPU 内存参与 | 必须，做 staging | 不需要 |
| NIC 访问方式 | CPU → socket → NIC | NIC 直接 DMA GPU 显存 |
| 数据路径 | GPU→PCIe→CPU→NIC→网络→远端CPU→PCIe→GPU | GPU→PCIe→NIC→网络→远端NIC→PCIe→GPU |

---

## 4. TCP 的 64KB Chunk 机制

TCP 传输时，数据被切成 64KB 的小块串行发送。核心是递归 async loop：

```cpp
void writeBody() {
    size_t buffer_size = min(64KB, remaining);
    if (buffer_size == 0) { /* 完成 */ return; }

    char *dram_buffer = addr + total_transferred_bytes_;  // 指针偏移
    async_write(socket_, buffer(dram_buffer, buffer_size), callback{
        total_transferred_bytes_ += transferred_bytes;
        writeBody();  // 递归发下一块
    });
}
```

### CPU 内存 vs GPU 内存的行为差异

**CPU 内存** — 不分配，不拷贝，只是指针推进：
```cpp
char *dram_buffer = addr + total_transferred_bytes_;  // 直接指向源 buffer 的偏移位置
asio::async_write(socket_, asio::buffer(dram_buffer, buffer_size), ...);
```
开销很小，本质就是普通 TCP 流式发送。

**GPU 内存** — 每块分配 + 拷贝 + 释放：
```cpp
dram_buffer = new char[buffer_size];          // 每次分配 64KB
cudaMemcpy(dram_buffer, addr + offset, ...);  // DtoH
async_write(...);
// 回调中 delete[] dram_buffer               // 每次释放
```
1MB 数据需要 16 次 `new` + `cudaMemcpy` + `delete`，效率很低。

### 为什么是 64KB

目的有三：
1. **控制内存占用**：无论传输多大，同一时刻只有 64KB 在线路上
2. **协作调度**：每块发完回调时 yield 回 io_context，让其他连接有机会推进
3. **GPU 内存处理**：每块单独 staging，不需要 pin 整块 GPU 内存

> **注意**：TCP 的 64KB chunk 大小是硬编码的（`kDefaultBufferSize = 65536`，tcp_transport.cpp:39），**不受 `MC_SLICE_SIZE` 环境变量影响**。`MC_SLICE_SIZE` 只对 RDMA 生效。

### TCP 连接管理

默认行为：每个 Slice 建一条新 TCP 连接，传完即关闭：

```cpp
void TcpTransport::startTransfer(Slice *slice) {
    asio::connect(socket, endpoint_iterator);  // 新建 TCP 连接
    auto session = std::make_shared<ClientSession>(std::move(socket));
    session->initiate(slice->source_addr, slice->length, ...);
}
// 传输完成后 socket 立即 close
```

可选连接池（`MC_TCP_ENABLE_CONNECTION_POOL=1`）：socket 按 endpoint 缓存复用，空闲 60 秒回收。服务端默认已在连接完成后复用 socket 等待下一个请求。

---

## 5. 非连续内存处理（Scatter-Gather）

### 核心数据结构层次

```
BatchDesc (一个 BatchID)
  └── TransferTask[] (每个对应一个 TransferRequest)
        └── Slice[] (每个 TransferRequest 被切成多个 Slice)
```

`TransferRequest` 本身是一对一连续区间：

```cpp
struct TransferRequest {
    enum OpCode { READ, WRITE };
    void *source;           // 一个连续源地址
    SegmentID target_id;    // 远端 segment 标识
    uint64_t target_offset; // 一个连续目标偏移
    size_t length;          // 长度
};
```

### Batch 级别 Scatter-Gather

要传多个非连续 buffer，在一个 batch 里提交多个 `TransferRequest`：

```cpp
vector<TransferRequest> entries;
entries.push_back({src1, target_seg, offset1, len1});
entries.push_back({src2, target_seg, offset2, len2});
entries.push_back({src3, target_seg, offset3, len3});
engine.submitTransfer(batch_id, entries);  // 一次提交，内部并发
```

单个 `TransferRequest` 不支持 scatter-gather，必须拆成多个 request。

### RDMA 侧：每个 Slice 一个 SGE，链表批量提交

```cpp
// 每个 Slice → 一个 ibv_send_wr + 一个 ibv_sge
sge.addr = (uint64_t)slice->source_addr;  // 可指向不同非连续地址
sge.length = slice->length;
sge.lkey = slice->rdma.source_lkey;
wr.num_sge = 1;
wr.sg_list = &sge;
wr.next = &next_wr;  // 链到下一个 slice

ibv_post_send(qp, &wr_head, &bad_wr);  // 一次提交整条链表
```

多个 Slice 通过 `wr.next` 链成链表，一次 `ibv_post_send` 批量提交到 NIC。

### 尾片合并优化

RDMA 传输时，如果最后一个 Slice 不足 64KB 但在 `64KB + 16KB` 以内，就合并到上一个 Slice，避免产生一个很小的尾片：

```cpp
const size_t kBlockSize = globalConfig().slice_size;  // 默认 65536，可通过 MC_SLICE_SIZE 配置
const size_t kFragmentSize = globalConfig().fragment_limit;  // 默认 16384，可通过 MC_FRAGMENT_LIMIT 配置

bool merge_final_slice = remaining <= kBlockSize + kFragmentSize;
slice->length = merge_final_slice ? remaining : kBlockSize;
```

### TCP 侧：多个 Slice 并发（协程式）

每个 Slice 独立建 TCP 连接，通过 `io_context` 的 async 回调协作式并发：

- 一个 Slice 在等 I/O 时，另一个 Slice 的回调可以推进
- 本质是单线程协程，不是真并行
- 不同 Slice 之间是并发的，但单个 Slice 内部是 64KB 串行的

---

## 6. 暴露给外部的 KVCache 传输接口

Mooncake 提供两个层次的 API：

### 底层：TransferEngine（点对点直接传输）

vLLM/SGLang 的 PD 分离场景使用。Python 接口：

```python
from mooncake.engine import TransferEngine

# 初始化（P2P 模式，不需要外部 metadata server）
engine = TransferEngine()
engine.initialize(hostname, "P2PHANDSHAKE", "rdma", "")

# 注册 GPU KV cache 内存（RDMA 场景调用 ibv_reg_mr / ibv_reg_dmabuf_mr）
engine.batch_register_memory(kv_data_ptrs, kv_data_lens)

# 批量同步传输（阻塞直到完成）
engine.batch_transfer_sync_write(
    target_session,         # 远端标识
    [gpu_layer0_addr, ...], # 源地址列表（GPU 显存）
    [remote_addr0, ...],    # 目标地址列表
    [len0, len1, ...]       # 每段长度
)

# 批量异步传输
batch_id = engine.batch_transfer_async_write(target, srcs, dsts, lengths)
status = engine.transfer_check_status(batch_id)

# 通用 opcode 参数化版本（支持 READ/WRITE）
batch_id = engine.batch_transfer_async(target, srcs, dsts, lengths, opcode)
status = engine.get_batch_transfer_status(batch_id)

# EFA 预连接优化（AWS 环境）
engine.warmup_efa_segment(target_session)
```

vLLM connector 实际调用流程（`mooncake_connector_v1.py`）：

1. 注册：`engine.batch_register_memory(kv_data_ptrs, kv_data_lens)`
2. 计算地址：`base_addr + block_id * block_len`，用 `group_concurrent_contiguous()` 合并相邻 block
3. 传输：`engine.batch_transfer_sync_write(remote_session, src_ptrs, dst_ptrs, lengths)`

### 上层：DistributedObjectStore（分布式 KV 存储语义）

带 master 管理、多副本、lease 一致性，适合把 KV cache 存到分布式池子里：

```python
from mooncake import DistributedObjectStore

store = DistributedObjectStore()
store.setup(...)

# 存取 tensor（自动序列化 dtype/shape）
store.put_tensor("cache/request_123/layer_0", k_tensor)
store.get_tensor("cache/request_123/layer_0")

# 零拷贝版本（直接操作注册过的 buffer 地址）
store.put_from("key", buffer_ptr, size)
store.get_into("key", buffer_ptr, size)

# scatter-gather：一个 key 对应多个不连续 buffer
store.batch_put_from_multi_buffers(keys, all_ptrs, all_sizes, config)
store.batch_get_into_multi_buffers(keys, all_ptrs, all_sizes)

# 幂等写入（upsert：存在则覆盖，不存在则创建）
store.upsert_tensor("key", tensor)
store.upsert_from("key", buffer_ptr, size)

# 多轴并行支持（TP/DP/EP/PP）
store.put_tensor_with_parallelism("key", tensor, parallelism_spec)
store.get_tensor_with_parallelism("key", read_target_spec)

# 跨 key/offset 的字节级范围读取
store.get_into_ranges(keys, buffer_ptrs, offsets, sizes)

# 持久化到 safetensor 格式
store.save_tensor_to_safetensor("key", "/path/to/file.safetensor")
store.load_tensor_from_safetensor("/path/to/file.safetensor")
```

### 两种 API 的适用场景

| 场景 | 推荐 API | 原因 |
|------|---------|------|
| PD 分离，preill → decode 直传 | TransferEngine | 零拷贝，最低延迟 |
| 分布式缓存池，多副本 | DistributedObjectStore | 自动副本管理、lease 一致性 |
| 需要持久化 KV cache | DistributedObjectStore | 支持 SSD 落盘 |

---

## 7. TCP 模式的性能瓶颈总结

`MC_FORCE_TCP` 本质上是 debug/兼容 fallback，性能代价很大：

| 瓶颈 | 说明 |
|------|------|
| GPU 路径 per-chunk alloc/free | 每 64KB 一次 `new` + `cudaMemcpy` + `delete`，可用 pinned memory + 预分配优化 |
| 单线程 io_context | 所有连接共用一个事件循环，无法利用多核 |
| 每 Slice 一条 TCP 连接 | 连接建立开销（三次握手）在大量小 Slice 时累积；可通过 `MC_TCP_ENABLE_CONNECTION_POOL=1` 缓解 |
| 无 pipeline | `async_write` 完成回调后才发下一块，没有 double-buffering |
| 无 GPU-Direct | 必须经过 CPU 内存 staging，多一次 PCIe 传输 |

对比 RDMA：直接 `ibv_post_send` 链表批量提交，NIC 硬件 DMA 直读 GPU 显存，没有 CPU 参与，没有 per-chunk 分配，吞吐量差几个数量级。

---

## 8. Transfer Engine 运行时指标（MC_TE_METRIC）

### 启用方式

设置环境变量 `MC_TE_METRIC=1`（也接受 `true`/`yes`/`on`），Transfer Engine 会启动一个后台线程，定期输出传输指标到 glog。

```
MC_TE_METRIC=1 MC_TE_METRIC_INTERVAL_SECONDS=5
```

- 采集间隔通过 `MC_TE_METRIC_INTERVAL_SECONDS` 配置，默认 5 秒
- 编译依赖 `WITH_METRICS=ON`（CMake 选项，默认已开启）
- **不支持 TENT**（TENT 有自己的独立指标系统，走 Prometheus HTTP endpoint）

### 采集的指标

| 指标 | 类型 | 说明 |
|------|------|------|
| `transferred_bytes_counter_` | counter | 成功传输的累计字节数，用于计算吞吐量 |
| `task_completion_latency_us_` | histogram | 每个 task 从提交到完成的延迟（微秒），19 个 bucket：10us ~ 10s |

### 数据埋点

- `submitTransfer()`：记录每个 task 的 `start_time`
- `getTransferStatus()`：task 完成时，累加字节数计数器，将延迟写入直方图

### 输出格式

glog `LOG(INFO)` 输出，示例：

```
[Metrics] Transfer Engine Stats (over last 5s): Throughput: 123.45 MB/s | Latency Distribution (count=1000): 0-10us:5.2%, 10-20us:12.3%, ..., >10000000us:0.1%
```

- 吞吐量：区间内传输字节数 / 间隔秒数
- 延迟分布：各 bucket 占比，低于 0.1% 的 bucket 不显示
- 区间内无传输活动时跳过输出

---

## 9. TENT 指标系统（Prometheus HTTP endpoint）

TENT（Transfer Engine New Technology）有独立的指标系统，编译开关 `TENT_METRICS_ENABLED`（默认 OFF），运行时通过环境变量启用：

```
TENT_METRICS_ENABLED=1 TENT_METRICS_HTTP_PORT=9100
```

### HTTP 端点

| 端点 | 格式 | 说明 |
|------|------|------|
| `GET /metrics` | Prometheus text | 可直接被 Prometheus 抓取 |
| `GET /metrics/summary` | 纯文本 | 人类可读摘要 |
| `GET /metrics/json` | JSON | 程序化消费 |
| `GET /health` | 纯文本 | 返回 "OK" |

### 采集的指标

| 指标 | 类型 | 说明 |
|------|------|------|
| `tent_read_bytes_total` | counter | TENT 读取总字节 |
| `tent_write_bytes_total` | counter | TENT 写入总字节 |
| `tent_read_requests_total` | counter | TENT 读请求总数 |
| `tent_write_requests_total` | counter | TENT 写请求总数 |
| `tent_read_failures_total` | counter | TENT 读失败总数 |
| `tent_write_failures_total` | counter | TENT 写失败总数 |
| `tent_transport_failover_total` | counter | 跨 transport 故障转移次数 |
| `tent_read_latency_us` | histogram | 读延迟（100us ~ 1s） |
| `tent_write_latency_us` | histogram | 写延迟（100us ~ 1s） |
| `tent_read_size_bytes` | histogram | 读请求大小分布（1KB ~ 1GB） |
| `tent_write_size_bytes` | histogram | 写请求大小分布（1KB ~ 1GB） |

### 与 MC_TE_METRIC 的区别

| 维度 | MC_TE_METRIC（Legacy） | TENT Metrics |
|------|----------------------|-------------|
| 编译开关 | `WITH_METRICS`（默认 ON） | `TENT_METRICS_ENABLED`（默认 OFF） |
| 输出方式 | glog 定期打印 | HTTP endpoint（Prometheus/JSON） |
| 指标粒度 | task 级别（粗） | read/write 分开（细） |
| 大小分布 | 无 | 有 histogram |
| 故障转移 | 无 | 有计数器 |
| 适用场景 | Legacy Transfer Engine | TENT |

---

## 10. Transfer Engine 核心配置参数

以下环境变量直接影响传输行为和性能，按功能分组。

### RDMA 调优

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `MC_SLICE_SIZE` | `65536` | 每个 Slice 大小（字节），**仅 RDMA 生效**，TCP 硬编码 64KB |
| `MC_FRAGMENT_RATIO` | `4` | 尾片合并阈值 = `slice_size / ratio`（默认 16KB） |
| `MC_NUM_QP_PER_EP` | `2` | 每个 endpoint 的 QP 数量，多个 QP 可提高并发 |
| `MC_MAX_WR` | `256` | 每个 QP 的最大 work request 数 |
| `MC_MAX_CQE_PER_CTX` | `4096` | 每个 context 的最大 CQ entry 数 |
| `MC_WORKERS_PER_CTX` | `2` | 每个 context 的 worker 线程数（1-8） |
| `MC_RETRY_CNT` | `9` | RDMA 重试次数（0-128） |
| `MC_SLICE_TIMEOUT` | `-1`（禁用） | Slice 传输超时（毫秒） |
| `MC_IB_PORT` | `1` | InfiniBand 端口号 |
| `MC_GID_INDEX` | 自动选择 | RDMA GID 索引，回退到 `NCCL_IB_GID_INDEX` |
| `MC_MTU` | `4096` | RDMA 路径 MTU（512/1024/2048/4096） |
| `MC_IB_TC` | `-1`（未设置） | InfiniBand traffic class（0-255） |
| `MC_IB_PCI_RELAXED_ORDERING` | `0` | PCI relaxed ordering：0=关，1=开，2=自动 |
| `MC_ENABLE_PARALLEL_REG_MR` | `-1`（自动） | 并行内存注册：-1=自动，0=关，1=开 |
| `MC_MAX_MR_SIZE` | ~1TB | 单个内存区域最大注册大小 |
| `MC_PATH_ROUNDROBIN` | `false` | RDMA 路径 round-robin 选择 |
| `MC_MLX5_QP_UDP_SPORTS` | 空 | mlx5 ECMP UDP 源端口（逗号分隔） |
| `MC_MLX5_QP_LAG_PORT_BALANCE` | `false` | mlx5 QP LAG 端口负载均衡 |

### TCP 配置

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `MC_FORCE_TCP` | `false` | 强制 TCP（跳过 RDMA） |
| `MC_TCP_ENABLE_CONNECTION_POOL` | 禁用 | TCP 连接池，设为 `1` 启用 |
| `MC_TCP_BIND_ADDRESS` | 自动检测 | TCP RPC 绑定地址 |
| `MC_HANDSHAKE_PORT` | `12001` | RDMA 握手 TCP 端口 |

### 传输控制

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `MC_TRANSFER_TIMEOUT` | `30`（秒） | 整体传输超时（最小 5 秒） |
| `MC_DISABLE_METACACHE` | 禁用 | 存在即禁用元数据缓存 |
| `MC_ENDPOINT_STORE_TYPE` | `SIEVE` | endpoint 缓存淘汰策略：`FIFO` 或 `SIEVE` |
| `MC_ENABLE_DEST_DEVICE_AFFINITY` | `false` | 目标设备亲和性 |
| `MC_FORCE_HCA` | `false` | 强制 HCA/RDMA transport |
| `MC_FORCE_MNNVL` | `false` | 强制多节点 NVLink |
| `MC_LOG_LEVEL` | `INFO` | 日志级别：`TRACE`/`INFO`/`WARNING`/`ERROR` |

### Transport 选择

| 环境变量 | 说明 |
|----------|------|
| `MC_USE_TENT` | 使用 TENT（新版 transport） |
| `MC_USE_TEV1` | 使用 Legacy transport |
| `MC_RPC_PROTOCOL` | RPC 协议，设 `rdma` 启用 RDMA-based RPC |
| `MC_CXL_DEV_PATH` | CXL 设备路径，设置后启用 CXL transport |
| `USE_BAREX` | 启用 Barex transport |

---

## 11. Mooncake Store 层指标与配置

### 三套指标系统概览

| 系统 | 位置 | 指标总数 | 输出方式 | 启用方式 |
|------|------|---------|---------|---------|
| TransferEngine Legacy | TE 核心 | 2 | glog | `MC_TE_METRIC=1` |
| TENT Metrics | TENT 子系统 | 11 | HTTP (Prometheus/JSON) | `TENT_METRICS_ENABLED=1` |
| Store Master | master 服务 | ~117 | HTTP `/metrics` | 默认开启 |
| Store Client | client 侧 | ~21 | glog + HTTP | `MC_STORE_CLIENT_METRIC=1` |
| Store HA | HA 子系统 | ~19 | HTTP `/metrics` | 随 HA 开启 |

### Store Master 指标（HTTP `/metrics`，默认端口 9003）

最值得关注的指标：

**容量与使用：**
- `master_allocated_bytes` / `master_total_capacity_bytes` — 内存分配量/总量
- `master_key_count` — 管理的 key 总数
- `master_active_clients` — 活跃客户端数
- `master_value_size_bytes` — 对象大小分布 histogram

**操作计数（每种 RPC 都有 requests + failures 计数器）：**
- `master_put_start_requests_total` / `_failures_total`
- `master_get_replica_list_requests_total` / `_failures_total`
- `master_batch_put_start_requests_total` / `_failures_total`（含 partial_successes/items/failed_items）

**缓存命中率：**
- `MEMORY_HIT_RATE` / `SSD_HIT_RATE` / `OVERALL_HIT_RATE` — 从 hit/total 计数器派生

**淘汰：**
- `master_successful_evictions_total` / `master_attempted_evictions_total`
- `master_evicted_key_count` / `master_evicted_size_bytes`

**HA 健康：**
- `ha_oplog_standby_lag` — Standby 落后 Primary 的 OpLog 条目数
- `ha_oplog_etcd_write_latency_us` — etcd 写入延迟
- `ha_standby_state` — Standby 状态机（0=STOPPED ~ 8=FAILED）

### Store Client 指标

| 指标 | 类型 | 说明 |
|------|------|------|
| `mooncake_transfer_read_bytes` / `_write_bytes` | counter | 传输字节 |
| `mooncake_transfer_batch_put_latency` / `_get_latency` | histogram | 批量操作延迟（us） |
| `mooncake_client_rpc_count` (label: rpc_name) | counter | 每种 RPC 调用次数 |
| `mooncake_client_rpc_latency` (label: rpc_name) | histogram | 每种 RPC 延迟 |
| `mooncake_ssd_read_latency_us` / `_write_latency_us` | histogram | SSD 读写延迟 |
| `mooncake_ssd_read_latency_summary_us` | summary | SSD 延迟分位数（p50/p90/p99） |

### Store 关键配置

**Master 配置（CLI flag / YAML）：**

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--default_kv_lease_ttl` | `5000ms` | KV 对象租约 TTL |
| `--eviction_ratio` | `0.05` | 内存满时淘汰比例 |
| `--eviction_high_watermark_ratio` | `0.95` | 触发淘汰的内存使用率 |
| `--client_live_ttl_sec` | `10` | 客户端存活判定（秒） |
| `--enable_ha` | `false` | 启用 HA |
| `--metrics_port` | `9003` | 指标 HTTP 端口 |
| `--rpc_thread_num` | `4` | RPC 线程数 |

**Client 环境变量：**

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `MC_STORE_CLIENT_METRIC` | `true` | 启用 client 指标 |
| `MC_STORE_CLIENT_METRIC_INTERVAL` | `0`（禁用） | client 指标定期输出间隔（秒） |
| `MC_STORE_CLUSTER_ID` | 空 | 集群标识，附加到所有指标 label |
| `MC_STORE_USE_HUGEPAGE` | 禁用 | hugepage 内存分配 |
| `MC_STORE_LOCAL_HOT_CACHE_SIZE` | `0`（禁用） | 本地热缓存大小 |
| `MC_STORE_LOCAL_HOT_BLOCK_SIZE` | `16MB` | 热缓存块大小 |
| `MC_STORE_MEMCPY` | 自动检测 | 本地 memcpy 优化 |

### 健康检查端点

**Master（默认端口 9003）：**

| 端点 | 返回 | 说明 |
|------|------|------|
| `GET /health` | JSON | `{"status":"ok","role":"...","ha_state":"...","service_ready":true}` |
| `GET /metrics` | Prometheus text | 所有 master + HA 指标 |
| `GET /metrics/summary` | 纯文本 | 人类可读摘要 |
| `GET /role` | 纯文本 | 当前角色 |
| `GET /ha_status` | 纯文本 | HA 状态 |
| `GET /leader` | JSON | leader 信息 |

**Client：**

| 端点 | 返回 | 说明 |
|------|------|------|
| `GET /health` | JSON | `{"healthy":true,"code":0}` 或 503 |
| `GET /metrics` | Prometheus text | client 指标 |

### Python 配置（mooncake-wheel）

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `MOONCAKE_CONFIG_PATH` | 无 | YAML/JSON 配置文件路径 |
| `MOONCAKE_MASTER` | 无 | master 地址 |
| `MOONCAKE_PROTOCOL` | `"tcp"` | 传输协议 |
| `MOONCAKE_DEVICE` | 空 | RDMA 设备名 |
| `VLLM_MOONCAKE_SIDE_CHANNEL_PORT` | `6557` | vLLM 侧信道端口 |
| `VLLM_MOONCAKE_PROTOCOL` | `"rdma"` | vLLM connector 协议 |

---

## 12. 数据切分、并发模型与内存路径详解

### 12.1 TCP：不切 Slice，64KB 流式递归

**切分策略：** TCP 在 transport 层 **不按 64KB 切 Slice**。一个 `TransferRequest` = 一个 Slice = 一个 `ClientSession`。64KB 分块发生在 `writeBody`/`readBody` 内部的递归回调中。

```
batch_transfer_sync([(src1,dst1,256KB), (src2,dst2,256KB), (src3,dst3,256KB)])
    ↓
  3 个 Slice → 3 个 ClientSession → 3 条 TCP 连接
```

**单个 Session 内部（串行递归）：**

```
Session 0 (256KB):
  [alloc 64KB] → DtoH → send → free →
  [alloc 64KB] → DtoH → send → free →
  [alloc 64KB] → DtoH → send → free →
  [alloc 64KB] → DtoH → send → free → done
```

同一时刻每个 Session 只持有 **1 个 64KB staging buffer**，发完释放后才分配下一个。无 double-buffering，无 pipeline。

**GPU 内存的完整数据路径（每个 64KB chunk）：**

```
发送端:                              接收端:
  ① new char[64KB]                   ① new char[64KB]
  ② cudaMemcpy(GPU → CPU) [DtoH]    ② async_read(socket → CPU buffer)
  ③ async_write(CPU → socket)        ③ cudaMemcpy(CPU → GPU) [HtoD]
  ④ delete[] (回调中)                 ④ delete[] (回调中)
```

每个 chunk 经历 **2 次 `new`/`delete` + 2 次 `cudaMemcpy`**（发送端 DtoH + 接收端 HtoD）。

**并发模型：**

- 多个 Session 通过 `io_context` 协作式并发（单线程 async 回调）
- 一个 Session 在等 I/O 时，另一个 Session 的回调可以推进
- N 个 Session 并发 = 同时 N 个 64KB staging buffer

**CPU 内存的差异：** 不分配 staging buffer，`dram_buffer = addr + offset` 直接用原始指针偏移，零分配。

---

### 12.2 RDMA：按 64KB 切 Slice，硬件批量并发

**切分策略：** RDMA 在 transport 层将每个 `TransferRequest` 按 64KB 切成多个 Slice：

```
batch_transfer_sync([(src1,dst1,256KB), (src2,dst2,256KB), (src3,dst3,256KB)])
    ↓
  3 个 Task → 每个切成 4 个 Slice → 共 12 个 Slice
```

**所有 Slice 汇总后批量提交：**

```
Task 0: Slice[0] Slice[1] Slice[2] Slice[3]
Task 1: Slice[4] Slice[5] Slice[6] Slice[7]
Task 2: Slice[8] Slice[9] Slice[10] Slice[11]
         ↓ 全部汇总
  slices_to_post (按 NIC 分组)
         ↓ 超过 kSubmitWatermark 时分批 flush
  ibv_post_send (链表批量提交)
```

**关键：不是每个 Task 单独提交，而是所有 Task 的所有 Slice 汇总后一起 post 到 NIC。**

**GPU 内存的完整数据路径：**

```
发送端 GPU                              接收端 GPU
  ┃                                       ┃
  ┃ PCIe DMA (GPU-Direct RDMA)            ┃ PCIe DMA
  ▼                                       ▲
RDMA NIC ───────── 网络 ─────────> 远端 RDMA NIC
```

无 staging buffer，无 `cudaMemcpy`，NIC 直接通过 PCIe DMA 读写 GPU 显存。

---

### 12.3 并发度对比

**RDMA 并发度：** 由 NIC 硬件参数决定

| 参数 | 默认值 | 环境变量 | 含义 |
|------|--------|----------|------|
| `max_wr` | 256 | `MC_MAX_WR` | 每个 QP 最大 in-flight WR 数 |
| `num_qp_per_ep` | 2 | `MC_NUM_QP_PER_EP` | 每个 endpoint 的 QP 数 |
| 并发度上限 | 512 | — | = max_wr × num_qp_per_ep |

- 12 个 Slice 一次 `ibv_post_send` 全部提交，NIC 硬件并行执行
- Slice 在多个 QP 间 round-robin 分配
- 受 `max_cqe`（默认 4096）限制 CQ 深度

**TCP 并发度：** 由 Session 数量决定

| 参数 | 默认值 | 环境变量 | 含义 |
|------|--------|----------|------|
| 并发度 | = Slice 数 | — | 每个 Slice 一个独立 Session |
| 连接池 | 禁用 | `MC_TCP_ENABLE_CONNECTION_POOL=1` | socket 复用 |

- N 个 Slice → N 个 Session → N 条 TCP 连接 → N 个 64KB buffer 同时在飞
- 单线程 `io_context`，协程式并发（不是真并行）
- 无显式并发上限

---

### 12.4 单次 batch_transfer 的端到端示例

以传输 3 个 256KB GPU buffer 为例：

**TCP：**

```
提交: 3 个 Slice, 3 个 Session 并发

Session 0: [new64K]→DtoH→send→free→[new64K]→DtoH→send→free→[new64K]→DtoH→send→free→[new64K]→DtoH→send→free
Session 1: [new64K]→DtoH→send→free→[new64K]→DtoH→send→free→[new64K]→DtoH→send→free→[new64K]→DtoH→send→free
Session 2: [new64K]→DtoH→send→free→[new64K]→DtoH→send→free→[new64K]→DtoH→send→free→[new64K]→DtoH→send→free

同一时刻: 最多 3 个 64KB CPU buffer (192KB)
总分配/释放次数: 3×4 = 12 次 new + 12 次 delete + 12 次 cudaMemcpy
```

**RDMA：**

```
提交: 12 个 Slice → 1 次 ibv_post_send → NIC 硬件并行

Slice[0] ─┐
Slice[1] ─┤
Slice[2] ─┤
Slice[3] ─┤
Slice[4] ─┼→ QP0: WR chain (6 个 WR)
Slice[5] ─┤
Slice[6] ─┤
Slice[7] ─┤
Slice[8] ─┤
Slice[9] ─┤
Slice[10]─┤→ QP1: WR chain (6 个 WR)
Slice[11]─┘

同一时刻: 0 个 CPU buffer，12 个 WR 在 NIC 硬件 in-flight
总分配/释放次数: 0 次 new/delete，0 次 cudaMemcpy
```
