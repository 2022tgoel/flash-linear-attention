# -*- coding: utf-8 -*-
# Copyright (c) 2023-2025, Songlin Yang, Yu Zhang

# "Eagle and Finch: RWKV with Matrix-Valued States and Dynamic Recurrence"[https://arxiv.org/abs/2404.05892]

from __future__ import annotations

import math
import warnings
from typing import TYPE_CHECKING, Optional, Tuple

import torch
import torch.nn as nn
from einops import rearrange

from fla.modules import GroupNorm
from fla.modules.activations import ACT2FN
from fla.modules.token_shift import token_shift
from fla.ops.rwkv6 import chunk_rwkv6, fused_recurrent_rwkv6

if TYPE_CHECKING:
    from fla.models.utils import Cache


class RWKV6Attention(nn.Module):

    def __init__(
        self,
        mode: str = 'chunk',
        hidden_size: int = 1024,
        expand_k: float = 0.5,
        expand_v: float = 1.0,
        num_heads: int = 4,
        gate_fn: str = 'swish',
        proj_low_rank_dim: int = 32,
        gate_low_rank_dim: int = 64,
        fuse_norm: bool = True,
        elementwise_affine: Optional[bool] = True,
        norm_eps: float = 1e-5,
        layer_idx: int = None,
        **kwargs
    ) -> RWKV6Attention:
        super().__init__()

        self.mode = mode
        self.hidden_size = hidden_size
        self.expand_k = expand_k
        self.expand_v = expand_v
        self.num_heads = num_heads
        self.proj_low_rank_dim = proj_low_rank_dim
        self.gate_low_rank_dim = gate_low_rank_dim

        self.key_dim = int(hidden_size * expand_k)
        self.value_dim = int(hidden_size * expand_v)
        self.layer_idx = layer_idx

        assert mode in ['chunk', 'fused_recurrent'], f"Not supported mode `{mode}`."
        assert self.key_dim % num_heads == 0, f"key dim must be divisible by num_heads of {num_heads}"
        assert self.value_dim % num_heads == 0, f"value dim must be divisible by num_heads of {num_heads}"

        self.head_k_dim = self.key_dim // num_heads
        self.head_v_dim = self.value_dim // num_heads

        self.time_shift = nn.ZeroPad2d((0, 0, 1, -1))
        self.x_proj = nn.Sequential(
            LerpLinear(hidden_size, proj_low_rank_dim * 5),
            nn.Tanh(),
            nn.Linear(proj_low_rank_dim * 5, hidden_size, bias=False)
        )
        self.x_bias = nn.Parameter(torch.zeros(5, hidden_size))

        self.r_proj = DDLerpLinear(hidden_size, self.key_dim)
        self.w_proj = DDLerpLinear(hidden_size, self.key_dim, low_rank_dim=gate_low_rank_dim)
        self.k_proj = DDLerpLinear(hidden_size, self.key_dim)
        self.v_proj = DDLerpLinear(hidden_size, self.value_dim)
        self.g_proj = DDLerpLinear(hidden_size, self.value_dim)
        self.bonus = nn.Parameter(torch.zeros(num_heads, self.head_k_dim))

        # TODO: fuse GroupNorm and output gate
        self.g_norm = GroupNorm(self.num_heads, self.value_dim, elementwise_affine=elementwise_affine, bias=True, eps=norm_eps)
        self.o_proj = nn.Linear(self.value_dim, hidden_size, bias=False)
        self.gate_fn = ACT2FN[gate_fn]

        try:
            from transformers.modeling_utils import _init_weights
        except ImportError:
            _init_weights = True
        if _init_weights:
            self.apply(self._initialize_weights)

        warnings.warn(
            "According to Bo, you are using a potentially buggy FLA implementation of RWKV. "
            "If you plan to report any numbers based on this implementation, we strongly recommend "
            "cross-checking with the official repo: https://github.com/BlinkDL/RWKV-LM. "
            "Bo may disagree with results reported from this version."
        )

    def _initialize_weights(self, module: nn.Module):
        if getattr(module, "_is_hf_initialized", False):
            return
        if isinstance(module, nn.Linear):
            nn.init.xavier_uniform_(module.weight, gain=2 ** -2.5)
            if module.bias is not None:
                nn.init.zeros_(module.bias)
        if isinstance(module, nn.Parameter):
            nn.init.xavier_uniform_(module, gain=2 ** -2.5)
        module._is_hf_initialized = True

    def forward(
        self,
        hidden_states: torch.Tensor,
        attention_mask: Optional[torch.Tensor] = None,
        past_key_values: Optional[Cache] = None,
        use_cache: Optional[bool] = False,
        output_attentions: Optional[bool] = False,
        cu_seqlens: Optional[torch.LongTensor] = None,
        **kwargs
    ) -> Tuple[torch.Tensor, Optional[torch.Tensor], Optional[Cache]]:
        if attention_mask is not None:
            assert len(attention_mask.shape) == 2, (
                "Expected attention_mask as a 0-1 matrix with shape [batch_size, seq_len] "
                "for padding purposes (0 indicating padding). "
                "Arbitrary attention masks of shape [batch_size, seq_len, seq_len] are not allowed."
            )

        batch_size, seq_len, hidden_size = hidden_states.shape
        # launching the triton kernel for just one token will actually be slower
        mode = 'fused_recurrent' if hidden_states.shape[1] <= 64 else self.mode

        last_state = None
        if past_key_values is not None and len(past_key_values) > self.layer_idx:
            last_state = past_key_values[self.layer_idx]

        if attention_mask is not None:
            hidden_states = hidden_states.mul_(attention_mask[:, -hidden_states.shape[-2]:, None])

        if hidden_states.shape[1] == 1 and last_state is not None:
            shifted = last_state['conv_state'].unsqueeze(1)
            delta = shifted - hidden_states
        elif last_state is None:
            delta = token_shift(hidden_states, cu_seqlens)
        else:
            shifted = self.time_shift(hidden_states)
            shifted[:, 0] = last_state['conv_state']
            delta = shifted - hidden_states

        x = self.x_proj[0](hidden_states, delta, cu_seqlens).view(batch_size, seq_len, -1, self.proj_low_rank_dim)
        x = torch.einsum('b t n r, h n r-> b t n h', self.x_proj[1](x), self.x_proj[2].weight.view(hidden_size, 5, -1))

        r, w, k, v, g = x.add_(self.x_bias).unbind(-2)
        r = self.r_proj(hidden_states, r, delta, cu_seqlens)
        w = self.w_proj(hidden_states, w, delta, cu_seqlens)
        k = self.k_proj(hidden_states, k, delta, cu_seqlens)
        v = self.v_proj(hidden_states, v, delta, cu_seqlens)
        g = self.g_proj(hidden_states, g, delta, cu_seqlens)

        # dealing with left-padding
        if attention_mask is not None:
            v = v.mul_(attention_mask[:, -v.shape[-2]:, None])
        r, w, k = map(lambda x: rearrange(x, 'b t (h d) -> b t h d', d=self.head_k_dim), (r, w, k))
        v = rearrange(v, 'b t (h d) -> b t h d', d=self.head_v_dim)
        w = -torch.exp(w)
        u = self.bonus

        recurrent_state = last_state['recurrent_state'] if last_state is not None else None

        if mode == 'fused_recurrent':
            o, recurrent_state = fused_recurrent_rwkv6(
                r=r,
                k=k,
                v=v,
                w=w,
                u=u,
                scale=1.,
                initial_state=recurrent_state,
                output_final_state=use_cache,
                cu_seqlens=cu_seqlens,
            )
        elif mode == 'chunk':
            o, recurrent_state = chunk_rwkv6(
                r=r,
                k=k,
                v=v,
                w=w,
                u=u,
                scale=1.,
                initial_state=recurrent_state,
                output_final_state=use_cache,
                cu_seqlens=cu_seqlens,
            )
        else:
            raise NotImplementedError(f"Not supported mode `{mode}`.")

        if past_key_values is not None:
            past_key_values.update(
                recurrent_state=recurrent_state,
                conv_state=hidden_states[:, -1],
                layer_idx=self.layer_idx,
                offset=r.shape[2]
            )

        o = self.g_norm(rearrange(o, '... h d -> ... (h d)')) * self.gate_fn(g)
        o = self.o_proj(o)

        return o, None, past_key_values


class LoRA(nn.Module):

    def __init__(
        self,
        input_dim: int,
        output_dim: int,
        low_rank_dim: int,
        bias: Optional[bool] = True,
        activation: Optional[str] = 'tanh'
    ):
        super().__init__()

        self.input_dim = input_dim
        self.output_dim = output_dim
        self.low_rank_dim = low_rank_dim
        self.bias = bias

        if activation is None:
            self.activation = nn.Identity()
        elif activation == 'sigmoid':
            self.activation = nn.Sigmoid()
        elif activation == 'tanh':
            self.activation = nn.Tanh()
        elif activation == 'relu':
            self.activation = nn.ReLU()
        else:
            raise ValueError(f"Not supported activation `{activation}`.")

        self.lora = nn.Sequential(
            nn.Linear(input_dim, low_rank_dim, bias=False),
            self.activation,
            nn.Linear(low_rank_dim, output_dim, bias=bias)
        )
        try:
            from transformers.modeling_utils import _init_weights
        except ImportError:
            _init_weights = True
        if _init_weights:
            self.apply(self._initialize_weights)

    def __repr__(self) -> str:
        s = f"{self.__class__.__name__}("
        s += f"input_dim={self.input_dim}, low_rank_dim={self.low_rank_dim}, output_dim={self.output_dim}"
        if not self.bias:
            s += f", bias={self.bias}"
        s += ")"
        return s

    def _initialize_weights(self, module: nn.Module):
        if getattr(module, "_is_hf_initialized", False):
            return

        # Initialize weights to zero as in original code
        nn.init.zeros_(self.lora[0].weight)
        original_dtype = self.lora[2].weight.dtype
        shape = self.lora[2].weight.shape
        # Convert to float32 for numerical stability in orthogonal init
        weight_fp32 = self.lora[2].weight.float()

        # Calculate gain based on dimensions
        gain = math.sqrt(shape[1] / shape[0]) if shape[1] > shape[0] else 1

        # Apply orthogonal initialization with scaling factor 0.1
        nn.init.orthogonal_(weight_fp32, gain=gain * 0.1)

        # Convert back to original dtype
        self.lora[2].weight.data.copy_(weight_fp32.to(original_dtype))
        # Set Lora[2] bias to zero
        if self.lora[2].bias is not None:
            nn.init.zeros_(self.lora[2].bias)

        module._is_hf_initialized = True

    def set_bias_value(self, value):
        """Set bias to a specific value (for v0, w0 etc.)"""
        if self.bias and self.lora[2].bias is not None:
            if isinstance(value, torch.Tensor):
                # Handle tensor values
                self.lora[2].bias.data.copy_(value.to(self.lora[2].bias.dtype))
            else:
                # Handle scalar values
                nn.init.constant_(self.lora[2].bias, value)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.lora(x)


class LerpLinear(nn.Module):

    def __init__(
        self,
        input_dim: int,
        output_dim: int,
        low_rank_dim: Optional[int] = None
    ):
        super().__init__()

        self.input_dim = input_dim
        self.output_dim = output_dim
        self.low_rank_dim = low_rank_dim

        self.time_shift = nn.ZeroPad2d((0, 0, 1, -1))
        if low_rank_dim is None:
            self.linear = nn.Linear(input_dim, output_dim, bias=False)
        else:
            self.linear = LoRA(input_dim, output_dim, low_rank_dim)
        self.mu = nn.Parameter(torch.zeros(input_dim))

    def __repr__(self) -> str:
        s = f"{self.__class__.__name__}({self.input_dim}, {self.output_dim}"
        if self.low_rank_dim is not None:
            s += f", low_rank_dim={self.low_rank_dim}"
        s += ")"
        return s

    def forward(self, x: torch.Tensor, delta: Optional[torch.Tensor] = None,
                cu_seqlens: Optional[torch.LongTensor] = None) -> torch.Tensor:
        if delta is None:
            delta = token_shift(x, cu_seqlens)
        return self.linear(x + delta * self.mu)


class DDLerpLinear(nn.Module):

    def __init__(
        self,
        input_dim: int,
        output_dim: int,
        low_rank_dim: Optional[int] = None
    ):
        super().__init__()

        self.input_dim = input_dim
        self.output_dim = output_dim
        self.low_rank_dim = low_rank_dim

        self.time_shift = nn.ZeroPad2d((0, 0, 1, -1))
        if low_rank_dim is None:
            self.linear = nn.Linear(input_dim, output_dim, bias=False)
        else:
            self.linear = LoRA(input_dim, output_dim, low_rank_dim)

    def __repr__(self) -> str:
        s = f"{self.__class__.__name__}({self.input_dim}, {self.output_dim}"
        if self.low_rank_dim is not None:
            s += f", low_rank_dim={self.low_rank_dim}"
        s += ")"
        return s

    def forward(self, x: torch.Tensor, mu: torch.Tensor,
                delta: Optional[torch.Tensor] = None,
                cu_seqlens: Optional[torch.LongTensor] = None) -> torch.Tensor:
        if delta is None:
            delta = token_shift(x, cu_seqlens)
        return self.linear(x + delta * mu)
