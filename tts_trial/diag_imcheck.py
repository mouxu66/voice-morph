import torch

print("base:", torch.is_grad_enabled(), torch.is_inference_mode_enabled())
torch.inference_mode().__enter__()
print("after temp __enter__:", torch.is_grad_enabled(), torch.is_inference_mode_enabled())
import gc
gc.collect()
print("after gc.collect():", torch.is_grad_enabled(), torch.is_inference_mode_enabled())

cm = torch.inference_mode()
cm.__enter__()
print("after held __enter__:", torch.is_grad_enabled(), torch.is_inference_mode_enabled())
gc.collect()
print("after gc.collect():", torch.is_grad_enabled(), torch.is_inference_mode_enabled())
cm.__exit__(None, None, None)
print("after cm.__exit__:", torch.is_grad_enabled(), torch.is_inference_mode_enabled())

torch.set_grad_enabled(False)
print("after set_grad_enabled(False):", torch.is_grad_enabled(),
      torch.is_inference_mode_enabled())
