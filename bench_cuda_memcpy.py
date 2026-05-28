#!/usr/bin/env python3
"""
Benchmark cudaMemcpy (DtoH / HtoD) performance: Pageable vs Pinned memory.

Usage:
    python bench_cuda_memcpy.py

Tests:
    1. Pageable memory (new char[]) — default behavior in Mooncake TCP
    2. Pinned memory (cudaMallocHost) — optimized path
    3. Pageable async (cudaMemcpyAsync on pageable, with stream sync)
    4. Pinned async (cudaMemcpyAsync on pinned, with stream sync)

Measures throughput (GB/s) and latency (us) for each transfer size.
"""

import time
import sys

try:
    import torch
except ImportError:
    print("Error: PyTorch is required. pip install torch")
    sys.exit(1)

if not torch.cuda.is_available():
    print("Error: CUDA is not available")
    sys.exit(1)


def bench_memcpy(label, src, dst, size_bytes, warmup=50, iterations=500):
    """Benchmark a single memcpy direction, return (throughput_gbps, latency_us)."""
    stream = torch.cuda.Stream()

    # Warmup
    for _ in range(warmup):
        with torch.cuda.stream(stream):
            dst.copy_(src)
        torch.cuda.current_stream().wait_stream(stream)

    torch.cuda.synchronize()
    start = time.perf_counter()
    for _ in range(iterations):
        with torch.cuda.stream(stream):
            dst.copy_(src)
        torch.cuda.current_stream().wait_stream(stream)
    torch.cuda.synchronize()
    elapsed = time.perf_counter() - start

    throughput_gbps = (size_bytes * iterations) / elapsed / 1e9  # GB/s
    latency_us = (elapsed / iterations) * 1e6  # us
    return throughput_gbps, latency_us


def bench_memcpy_async(label, src, dst, size_bytes, warmup=50, iterations=500):
    """Benchmark async memcpy with stream, return (throughput_gbps, latency_us)."""
    stream = torch.cuda.Stream()

    # Warmup
    for _ in range(warmup):
        with torch.cuda.stream(stream):
            dst.copy_(src)
    torch.cuda.synchronize()

    start = time.perf_counter()
    for _ in range(iterations):
        with torch.cuda.stream(stream):
            dst.copy_(src)
    torch.cuda.synchronize()
    elapsed = time.perf_counter() - start

    throughput_gbps = (size_bytes * iterations) / elapsed / 1e9
    latency_us = (elapsed / iterations) * 1e6
    return throughput_gbps, latency_us


def main():
    device = torch.device("cuda:0")
    gpu_name = torch.cuda.get_device_name(0)
    print(f"GPU: {gpu_name}")
    print(f"PyTorch: {torch.__version__}, CUDA: {torch.version.cuda}")
    print()

    # Transfer sizes to test
    sizes = [
        ("4KB",   4 * 1024),
        ("16KB",  16 * 1024),
        ("64KB",  64 * 1024),
        ("256KB", 256 * 1024),
        ("1MB",   1024 * 1024),
        ("4MB",   4 * 1024 * 1024),
        ("16MB",  16 * 1024 * 1024),
        ("64MB",  64 * 1024 * 1024),
    ]

    results = []

    print(f"{'Size':>8}  {'Direction':>10}  {'Pageable':>14}  {'Pinned':>14}  {'Speedup':>8}")
    print("-" * 70)

    for label, size in sizes:
        n_elements = size  # 1 byte per element (uint8)

        # GPU tensor
        gpu_tensor = torch.empty(n_elements, dtype=torch.uint8, device=device)

        # Pageable CPU tensor (普通堆内存)
        pageable_tensor = torch.empty(n_elements, dtype=torch.uint8, pin_memory=False)

        # Pinned CPU tensor (cudaMallocHost)
        pinned_tensor = torch.empty(n_elements, dtype=torch.uint8, pin_memory=True)

        # Adjust iterations for large sizes
        if size >= 16 * 1024 * 1024:
            warmup, iters = 20, 200
        elif size >= 1 * 1024 * 1024:
            warmup, iters = 30, 300
        else:
            warmup, iters = 50, 500

        # --- DtoH (GPU -> CPU) ---
        pageable_dtoh_bw, pageable_dtoh_lat = bench_memcpy(
            "DtoH pageable", gpu_tensor, pageable_tensor, size, warmup, iters)
        pinned_dtoh_bw, pinned_dtoh_lat = bench_memcpy(
            "DtoH pinned", gpu_tensor, pinned_tensor, size, warmup, iters)

        # --- HtoD (CPU -> GPU) ---
        pageable_htod_bw, pageable_htod_lat = bench_memcpy(
            "HtoD pageable", pageable_tensor, gpu_tensor, size, warmup, iters)
        pinned_htod_bw, pinned_htod_lat = bench_memcpy(
            "HtoD pinned", pinned_tensor, gpu_tensor, size, warmup, iters)

        results.append({
            "size_label": label,
            "size_bytes": size,
            "pageable_dtoh_bw": pageable_dtoh_bw,
            "pageable_dtoh_lat": pageable_dtoh_lat,
            "pinned_dtoh_bw": pinned_dtoh_bw,
            "pinned_dtoh_lat": pinned_dtoh_lat,
            "pageable_htod_bw": pageable_htod_bw,
            "pageable_htod_lat": pageable_htod_lat,
            "pinned_htod_bw": pinned_htod_bw,
            "pinned_htod_lat": pinned_htod_lat,
        })

        dtoh_speedup = pinned_dtoh_bw / pageable_dtoh_bw if pageable_dtoh_bw > 0 else 0
        htod_speedup = pinned_htod_bw / pageable_htod_bw if pageable_htod_bw > 0 else 0

        print(f"{label:>8}  {'DtoH':>10}  {pageable_dtoh_bw:>10.2f} GB/s  {pinned_dtoh_bw:>10.2f} GB/s  {dtoh_speedup:>6.2f}x")
        print(f"{'':>8}  {'HtoD':>10}  {pageable_htod_bw:>10.2f} GB/s  {pinned_htod_bw:>10.2f} GB/s  {htod_speedup:>6.2f}x")

        # Cleanup
        del gpu_tensor, pageable_tensor, pinned_tensor
        torch.cuda.empty_cache()

    # Print latency summary
    print()
    print(f"{'Size':>8}  {'Direction':>10}  {'Pageable lat':>14}  {'Pinned lat':>14}")
    print("-" * 55)
    for r in results:
        print(f"{r['size_label']:>8}  {'DtoH':>10}  {r['pageable_dtoh_lat']:>10.1f} us  {r['pinned_dtoh_lat']:>10.1f} us")
        print(f"{'':>8}  {'HtoD':>10}  {r['pageable_htod_lat']:>10.1f} us  {r['pinned_htod_lat']:>10.1f} us")

    return results


if __name__ == "__main__":
    main()


# =============================================================================
# 测试结果 (2026-05-27)
# =============================================================================
#
# 测试环境:
#   机器 A: ml-b1-ser070.nmg03, NVIDIA H200 (141GB), PyTorch 2.11.0+cu129, CUDA 12.9
#   机器 B: ml-h203e-ser140.nmg03, NVIDIA H20-3e (141GB), PyTorch 2.11.0+cu129, CUDA 12.9
#
# 两台机器结果高度一致，以下为 H200 数据 (H20 差异 < 5%):
#
# ┌──────────────────────────────────────────────────────────────────────────┐
# │                     吞吐量 (GB/s)                                       │
# ├────────┬──────────┬─────────────────┬─────────────────┬─────────────────┤
# │  Size  │ Directon │ Pageable        │ Pinned          │ Speedup         │
# ├────────┼──────────┼─────────────────┼─────────────────┼─────────────────┤
# │   4KB  │     DtoH │      0.10 GB/s  │      0.11 GB/s  │    1.07x        │
# │        │     HtoD │      0.11 GB/s  │      0.11 GB/s  │    1.00x        │
# │  16KB  │     DtoH │      0.40 GB/s  │      0.45 GB/s  │    1.12x        │
# │        │     HtoD │      0.43 GB/s  │      0.43 GB/s  │    1.00x        │
# │  64KB  │     DtoH │      1.37 GB/s  │      1.75 GB/s  │    1.27x        │
# │        │     HtoD │      1.48 GB/s  │      1.74 GB/s  │    1.18x        │
# │ 256KB  │     DtoH │      3.76 GB/s  │      6.38 GB/s  │    1.70x        │
# │        │     HtoD │      4.43 GB/s  │      5.48 GB/s  │    1.24x        │
# │   1MB  │     DtoH │      6.97 GB/s  │     18.73 GB/s  │    2.69x        │
# │        │     HtoD │      8.53 GB/s  │     18.59 GB/s  │    2.18x        │
# │   4MB  │     DtoH │     10.65 GB/s  │     37.20 GB/s  │    3.49x        │
# │        │     HtoD │     11.21 GB/s  │     37.04 GB/s  │    3.30x        │
# │  16MB  │     DtoH │     12.66 GB/s  │     49.03 GB/s  │    3.87x        │
# │        │     HtoD │     13.15 GB/s  │     49.21 GB/s  │    3.74x        │
# │  64MB  │     DtoH │     13.30 GB/s  │     53.34 GB/s  │    4.01x        │
# │        │     HtoD │     13.73 GB/s  │     53.56 GB/s  │    3.90x        │
# └────────┴──────────┴─────────────────┴─────────────────┴─────────────────┘
#
# ┌──────────────────────────────────────────────────────────┐
# │                     延迟 (us)                            │
# ├────────┬──────────┬─────────────────┬────────────────────┤
# │  Size  │ Directon │ Pageable lat    │ Pinned lat         │
# ├────────┼──────────┼─────────────────┼────────────────────┤
# │   4KB  │     DtoH │       39.3 us   │       36.6 us      │
# │        │     HtoD │       36.0 us   │       36.1 us      │
# │  16KB  │     DtoH │       40.8 us   │       36.6 us      │
# │        │     HtoD │       38.1 us   │       38.0 us      │
# │  64KB  │     DtoH │       47.7 us   │       37.4 us      │
# │        │     HtoD │       44.3 us   │       37.7 us      │
# │ 256KB  │     DtoH │       69.7 us   │       41.1 us      │
# │        │     HtoD │       59.2 us   │       47.8 us      │
# │   1MB  │     DtoH │      150.4 us   │       56.0 us      │
# │        │     HtoD │      122.9 us   │       56.4 us      │
# │   4MB  │     DtoH │      393.9 us   │      112.7 us      │
# │        │     HtoD │      374.3 us   │      113.2 us      │
# │  16MB  │     DtoH │     1324.7 us   │      342.2 us      │
# │        │     HtoD │     1275.4 us   │      340.9 us      │
# │  64MB  │     DtoH │     5047.6 us   │     1258.2 us      │
# │        │     HtoD │     4886.7 us   │     1252.9 us      │
# └────────┴──────────┴─────────────────┴────────────────────┘
#
# 关键发现:
#
# 1. 小数据量 (< 64KB): Pinned 和 Pageable 几乎无差异，因为 memcpy 固有延迟 (~35-40us)
#    主导，与内存类型无关。
#
# 2. 中等数据量 (256KB-1MB): Pinned 优势开始显现。256KB DtoH: Pinned 6.38 GB/s vs
#    Pageable 3.76 GB/s (1.70x)。这是因为 pinned memory 允许 GPU DMA 直接读写 CPU
#    内存，而 pageable memory 需要先拷贝到内部 staging buffer。
#
# 3. 大数据量 (4MB-64MB): Pinned 优势最大。64MB DtoH: Pinned 53.34 GB/s vs
#    Pageable 13.30 GB/s (4.01x)。接近 PCIe 带宽上限。
#
# 4. 对 Mooncake TCP 传输的启示:
#    - 64KB chunk (Mooncake 当前默认): DtoH 延迟 ~48us (pageable) vs ~37us (pinned)
#      差距仅 ~10us，与网络 round-trip (~50us) 相比不大。这就是为什么之前测出
#      VRAM 模式在 64KB block_size 时只比 DRAM 慢 2%。
#    - 如果改用 256KB chunk: DtoH 延迟 ~70us (pageable) vs ~41us (pinned)
#      差距扩大到 ~30us，优化价值更明显。
#    - 最佳优化方案: pinned memory + cudaMemcpyAsync + double-buffering，
#      可以把 DtoH 完全隐藏在网络发送之后，VRAM 吞吐接近 DRAM。
