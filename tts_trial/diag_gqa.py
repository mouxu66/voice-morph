"""验证 sdpa enable_gqa + bool mask：正确性 + 零泄漏。"""
import torch

DEV = "cuda:0"
torch.manual_seed(0)
torch.inference_mode().__enter__()

q = torch.randn(1, 16, 1, 128, dtype=torch.bfloat16, device=DEV)
k = torch.randn(1, 8, 2048, 128, dtype=torch.bfloat16, device=DEV)
v = torch.randn(1, 8, 2048, 128, dtype=torch.bfloat16, device=DEV)
mask = torch.zeros(1, 1, 1, 2048, dtype=torch.bool, device=DEV)
mask[..., :100] = True  # 只看前 100 个 kv


def alloc():
    torch.cuda.synchronize()
    return torch.cuda.memory_allocated() / 2**30


# 基准：materialized repeat 路径
k_rep = k[:, :, None, :, :].expand(1, 8, 2, 2048, 128).reshape(1, 16, 2048, 128)
v_rep = v[:, :, None, :, :].expand(1, 8, 2, 2048, 128).reshape(1, 16, 2048, 128)
out_ref = torch.nn.functional.scaled_dot_product_attention(
    q, k_rep, v_rep, attn_mask=mask)
print("ref path ok, alloc:", round(alloc(), 3), flush=True)

# GQA 路径
out_gqa = torch.nn.functional.scaled_dot_product_attention(
    q, k, v, attn_mask=mask, enable_gqa=True)
diff = (out_ref.float() - out_gqa.float()).abs().max().item()
print(f"gqa max diff vs ref: {diff}", flush=True)

# 泄漏测试：GQA 循环
base = alloc()
for _ in range(20):
    o = torch.nn.functional.scaled_dot_product_attention(
        q, k, v, attn_mask=mask, enable_gqa=True)
    del o
torch.cuda.synchronize()
print(f"gqa loop 20x delta alloc: {(alloc()-base)*1024:.1f} MB", flush=True)

# 对照：materialized repeat 循环（带 mask，模拟 sdpa_attention_forward 真实调用形态）
for _ in range(20):
    kr = k[:, :, None, :, :].expand(1, 8, 2, 2048, 128).reshape(1, 16, 2048, 128)
    vr = v[:, :, None, :, :].expand(1, 8, 2, 2048, 128).reshape(1, 16, 2048, 128)
    o = torch.nn.functional.scaled_dot_product_attention(q, kr, vr, attn_mask=mask)
    del o, kr, vr
torch.cuda.synchronize()
print(f"repeat loop 20x delta alloc: {(alloc()-base)*1024:.1f} MB", flush=True)
