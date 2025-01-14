""" CLIP Model

Adapted from https://github.com/openai/CLIP. Originally MIT License, Copyright (c) 2021 OpenAI.
"""
from collections import OrderedDict
from dataclasses import dataclass
import logging
import math
from typing import Tuple, List, Union, Callable, Optional

import numpy as np
import torch
import torch.nn.functional as F
from torch import nn
from torch.utils.checkpoint import checkpoint

from .timm_model import TimmModel
from .utils import freeze_batch_norm_2d, to_2tuple


class Bottleneck(nn.Module):
    expansion = 4

    def __init__(self, inplanes, planes, stride=1):
        super().__init__()

        # all conv layers have stride 1. an avgpool is performed after the second convolution when stride > 1
        self.conv1 = nn.Conv2d(inplanes, planes, 1, bias=False)
        self.bn1 = nn.BatchNorm2d(planes)
        self.relu1 = nn.ReLU(inplace=True)

        self.conv2 = nn.Conv2d(planes, planes, 3, padding=1, bias=False)
        self.bn2 = nn.BatchNorm2d(planes)
        self.relu2 = nn.ReLU(inplace=True)

        self.avgpool = nn.AvgPool2d(stride) if stride > 1 else nn.Identity()

        self.conv3 = nn.Conv2d(planes, planes * self.expansion, 1, bias=False)
        self.bn3 = nn.BatchNorm2d(planes * self.expansion)
        self.relu3 = nn.ReLU(inplace=True)

        self.downsample = None
        self.stride = stride

        if stride > 1 or inplanes != planes * Bottleneck.expansion:
            # downsampling layer is prepended with an avgpool, and the subsequent convolution has stride 1
            self.downsample = nn.Sequential(OrderedDict([
                ("-1", nn.AvgPool2d(stride)),
                ("0", nn.Conv2d(inplanes, planes * self.expansion, 1, stride=1, bias=False)),
                ("1", nn.BatchNorm2d(planes * self.expansion))
            ]))

    def forward(self, x: torch.Tensor):
        identity = x

        out = self.relu1(self.bn1(self.conv1(x)))
        out = self.relu2(self.bn2(self.conv2(out)))
        out = self.avgpool(out)
        out = self.bn3(self.conv3(out))

        if self.downsample is not None:
            identity = self.downsample(x)

        out += identity
        out = self.relu3(out)
        return out


class AttentionPool2d(nn.Module):
    def __init__(self, spacial_dim: int, embed_dim: int, num_heads: int, output_dim: int = None):
        super().__init__()
        self.positional_embedding = nn.Parameter(torch.randn(spacial_dim ** 2 + 1, embed_dim) / embed_dim ** 0.5)
        self.k_proj = nn.Linear(embed_dim, embed_dim)
        self.q_proj = nn.Linear(embed_dim, embed_dim)
        self.v_proj = nn.Linear(embed_dim, embed_dim)
        self.c_proj = nn.Linear(embed_dim, output_dim or embed_dim)
        self.num_heads = num_heads

    def forward(self, x):
        x = x.reshape(x.shape[0], x.shape[1], x.shape[2] * x.shape[3]).permute(2, 0, 1)  # NCHW -> (HW)NC
        x = torch.cat([x.mean(dim=0, keepdim=True), x], dim=0)  # (HW+1)NC
        x = x + self.positional_embedding[:, None, :].to(x.dtype)  # (HW+1)NC
        x, _ = F.multi_head_attention_forward(
            query=x, key=x, value=x,
            embed_dim_to_check=x.shape[-1],
            num_heads=self.num_heads,
            q_proj_weight=self.q_proj.weight,
            k_proj_weight=self.k_proj.weight,
            v_proj_weight=self.v_proj.weight,
            in_proj_weight=None,
            in_proj_bias=torch.cat([self.q_proj.bias, self.k_proj.bias, self.v_proj.bias]),
            bias_k=None,
            bias_v=None,
            add_zero_attn=False,
            dropout_p=0,
            out_proj_weight=self.c_proj.weight,
            out_proj_bias=self.c_proj.bias,
            use_separate_proj_weight=True,
            training=self.training,
            need_weights=False
        )

        return x[0]


class ModifiedResNet(nn.Module):
    """
    A ResNet class that is similar to torchvision's but contains the following changes:
    - There are now 3 "stem" convolutions as opposed to 1, with an average pool instead of a max pool.
    - Performs anti-aliasing strided convolutions, where an avgpool is prepended to convolutions with stride > 1
    - The final pooling layer is a QKV attention instead of an average pool
    """

    def __init__(self, layers, output_dim, heads, image_size=224, width=64):
        super().__init__()
        self.output_dim = output_dim
        self.image_size = image_size

        # the 3-layer stem
        self.conv1 = nn.Conv2d(3, width // 2, kernel_size=3, stride=2, padding=1, bias=False)
        self.bn1 = nn.BatchNorm2d(width // 2)
        self.relu1 = nn.ReLU(inplace=True)
        self.conv2 = nn.Conv2d(width // 2, width // 2, kernel_size=3, padding=1, bias=False)
        self.bn2 = nn.BatchNorm2d(width // 2)
        self.relu2 = nn.ReLU(inplace=True)
        self.conv3 = nn.Conv2d(width // 2, width, kernel_size=3, padding=1, bias=False)
        self.bn3 = nn.BatchNorm2d(width)
        self.relu3 = nn.ReLU(inplace=True)
        self.avgpool = nn.AvgPool2d(2)

        # residual layers
        self._inplanes = width  # this is a *mutable* variable used during construction
        self.layer1 = self._make_layer(width, layers[0])
        self.layer2 = self._make_layer(width * 2, layers[1], stride=2)
        self.layer3 = self._make_layer(width * 4, layers[2], stride=2)
        self.layer4 = self._make_layer(width * 8, layers[3], stride=2)

        embed_dim = width * 32  # the ResNet feature dimension
        self.attnpool = AttentionPool2d(image_size // 32, embed_dim, heads, output_dim)

        self.init_parameters()

    def _make_layer(self, planes, blocks, stride=1):
        layers = [Bottleneck(self._inplanes, planes, stride)]

        self._inplanes = planes * Bottleneck.expansion
        for _ in range(1, blocks):
            layers.append(Bottleneck(self._inplanes, planes))

        return nn.Sequential(*layers)

    def init_parameters(self):
        if self.attnpool is not None:
            std = self.attnpool.c_proj.in_features ** -0.5
            nn.init.normal_(self.attnpool.q_proj.weight, std=std)
            nn.init.normal_(self.attnpool.k_proj.weight, std=std)
            nn.init.normal_(self.attnpool.v_proj.weight, std=std)
            nn.init.normal_(self.attnpool.c_proj.weight, std=std)

        for resnet_block in [self.layer1, self.layer2, self.layer3, self.layer4]:
            for name, param in resnet_block.named_parameters():
                if name.endswith("bn3.weight"):
                    nn.init.zeros_(param)

    def lock(self, unlocked_groups=0, freeze_bn_stats=False):
        assert unlocked_groups == 0, 'partial locking not currently supported for this model'
        for param in self.parameters():
            param.requires_grad = False
        if freeze_bn_stats:
            freeze_batch_norm_2d(self)

    @torch.jit.ignore
    def set_grad_checkpointing(self, enable=True):
        # FIXME support for non-transformer
        pass

    def stem(self, x):
        x = self.relu1(self.bn1(self.conv1(x)))
        x = self.relu2(self.bn2(self.conv2(x)))
        x = self.relu3(self.bn3(self.conv3(x)))
        x = self.avgpool(x)
        return x

    def forward(self, x):
        x = self.stem(x)
        x = self.layer1(x)
        x = self.layer2(x)
        x = self.layer3(x)
        x = self.layer4(x)
        x = self.attnpool(x)

        return x


class LayerNorm(nn.LayerNorm):
    """Subclass torch's LayerNorm to handle fp16."""

    def forward(self, x: torch.Tensor):
        orig_type = x.dtype
        x = F.layer_norm(x, self.normalized_shape, self.weight, self.bias, self.eps)
        return x.to(orig_type)


class QuickGELU(nn.Module):
    # NOTE This is slower than nn.GELU or nn.SiLU and uses more GPU memory
    def forward(self, x: torch.Tensor):
        return x * torch.sigmoid(1.702 * x)


class ResidualAttentionBlock(nn.Module):
    def __init__(self, d_model: int, n_head: int, mlp_ratio: float = 4.0, act_layer: Callable = nn.GELU, dropout: float = 0.0):
        super().__init__()

        self.attn = nn.MultiheadAttention(d_model, n_head)
        self.ln_1 = LayerNorm(d_model)
        mlp_width = int(d_model * mlp_ratio)
        layers = [("c_fc", nn.Linear(d_model, mlp_width))]
        if dropout > 0.0:
            layers.append(("c_drop", nn.Dropout(dropout)))
        layers.extend([("c_act", act_layer()), ("proj", nn.Linear(mlp_width, d_model))])
        self.mlp = nn.Sequential(OrderedDict(layers))
        self.ln_2 = LayerNorm(d_model)

    def attention(self, x: torch.Tensor, attn_mask: Optional[torch.Tensor] = None):
        return self.attn(x, x, x, need_weights=False, attn_mask=attn_mask)[0]

    def forward(self, x: torch.Tensor, attn_mask: Optional[torch.Tensor] = None):
        x = x + self.attention(self.ln_1(x), attn_mask=attn_mask)
        x = x + self.mlp(self.ln_2(x))
        return x


class Transformer(nn.Module):
    def __init__(self, width: int, layers: int, heads: int,  mlp_ratio: float = 4.0, act_layer: Callable = nn.GELU, dropout: float = 0.0):
        super().__init__()
        self.width = width
        self.layers = layers
        self.grad_checkpointing = False

        self.resblocks = nn.ModuleList([
            ResidualAttentionBlock(width, heads, mlp_ratio, act_layer=act_layer, dropout=dropout)
            for _ in range(layers)
        ])

    def forward(self, x: torch.Tensor, attn_mask: Optional[torch.Tensor] = None):
        for r in self.resblocks:
            if self.grad_checkpointing and not torch.jit.is_scripting():
                x = checkpoint(r, x, attn_mask)
            else:
                x = r(x, attn_mask=attn_mask)
        return x


class VisualTransformer3d(nn.Module):
    def __init__(
        self, space_size: List[int], patch_size: int, width: int, layers: int, heads: int, mlp_ratio: float,
        output_dim: int, act_layer: Callable = nn.GELU, dropout: float = 0.0
    ):
        super().__init__()
        self.space_size = space_size  # [H, W, D]
        self.patch_size = patch_size  # [h, w, d]
        self.grid_size = (space_size[0] // patch_size, space_size[1] // patch_size, space_size[2] // patch_size)
        self.output_dim = output_dim
        self.conv1 = nn.Conv3d(1, width, kernel_size=patch_size, stride=patch_size, bias=False)

        scale = width ** -0.5
        self.class_embedding = nn.Parameter(scale * torch.randn(width))
        self.position_embedding = nn.Parameter(scale * torch.randn(self.grid_size[0] * self.grid_size[1] * self.grid_size[2] + 1, width))
        self.ln_pre = LayerNorm(width)

        self.transformer = Transformer(width, layers, heads, mlp_ratio, act_layer, dropout)
        
        self.ln_post = LayerNorm(width)
        self.proj = nn.Parameter(scale * torch.randn(width, output_dim))

    def forward(self, x: torch.Tensor):
        tmp = x
        x = self.conv1(x) # [*, width, grid_size[0], grid_size[1], grid_size[2]]
        x = x.reshape(x.shape[0], x.shape[1], -1) # [*, width, grid_size[0] * grid_size[1] * grid_size[2]] := [*, width, size]
        x = x.permute(0, 2, 1) # [*, size, width]
        x = torch.cat([
            self.class_embedding.to(x.dtype) + torch.zeros(x.shape[0], 1, x.shape[-1], dtype=x.dtype, device=x.device),
            x
        ], dim=1) # [*, size + 1, width]
        x = x + self.position_embedding.to(x.dtype) # [*, size + 1, width]
        x = self.ln_pre(x)

        x = x.permute(1, 0, 2)  # NLD -> LND
        x = self.transformer(x)
        x = x.permute(1, 0, 2)  # LND -> NLD

        x = self.ln_post(x[:, 0, :])

        if self.proj is not None:
            x = x @ self.proj

        return x

class VisualTransformer(nn.Module):
    def __init__(
            self, image_size: int, patch_size: int, width: int, layers: int, heads: int, mlp_ratio: float,
            output_dim: int, act_layer: Callable = nn.GELU, input_channels: int = 3, dropout: float = 0.0):
        super().__init__()
        print(f"Transformer of width {width} and {layers} layers with {heads} heads")
        self.image_size = to_2tuple(image_size)
        self.patch_size = to_2tuple(patch_size)
        self.grid_size = (self.image_size[0] // self.patch_size[0], self.image_size[1] // self.patch_size[1])
        self.output_dim = output_dim
        self.conv1 = nn.Conv2d(in_channels=input_channels, out_channels=width, kernel_size=patch_size, stride=patch_size, bias=False)

        scale = width ** -0.5
        self.class_embedding = nn.Parameter(scale * torch.randn(width))
        self.positional_embedding = nn.Parameter(scale * torch.randn(self.grid_size[0] * self.grid_size[1] + 1, width))
        self.ln_pre = LayerNorm(width)

        self.transformer = Transformer(width, layers, heads, mlp_ratio, act_layer=act_layer, dropout=dropout)

        self.ln_post = LayerNorm(width)
        self.proj = nn.Parameter(scale * torch.randn(width, output_dim))

    def lock(self, unlocked_groups=0, freeze_bn_stats=False):
        assert unlocked_groups == 0, 'partial locking not currently supported for this model'
        for param in self.parameters():
            param.requires_grad = False

    @torch.jit.ignore
    def set_grad_checkpointing(self, enable=True):
        self.transformer.grad_checkpointing = enable

    def forward(self, x: torch.Tensor):
        x = self.conv1(x)  # shape = [*, width, grid, grid]
        x = x.reshape(x.shape[0], x.shape[1], -1)  # shape = [*, width, grid ** 2]
        x = x.permute(0, 2, 1)  # shape = [*, grid ** 2, width]
        x = torch.cat(
            [self.class_embedding.to(x.dtype) + torch.zeros(x.shape[0], 1, x.shape[-1], dtype=x.dtype, device=x.device),
             x], dim=1)  # shape = [*, grid ** 2 + 1, width]
        x = x + self.positional_embedding.to(x.dtype)
        x = self.ln_pre(x)

        x = x.permute(1, 0, 2)  # NLD -> LND
        x = self.transformer(x)
        x = x.permute(1, 0, 2)  # LND -> NLD

        x = self.ln_post(x[:, 0, :])

        if self.proj is not None:
            x = x @ self.proj

        return x

class Voxel2DVisualTransformer(VisualTransformer):
    # Wraps a visual transformer to handle voxel data
    def __init__(self, image_size: int, channel_dim: int = 2, *args, **kwargs):
        super().__init__(image_size=image_size, *args, **kwargs)
        self.channel_dim = channel_dim
        self.image_size = image_size

    def forward(self, x: torch.Tensor):
        # x.shape = [*, channel_0, channel_1, channel_2]
        other_dims = [i+1 for i in filter(lambda x: x != self.channel_dim, [0, 1, 2])]
        # print(f"Permuted to {(0, self.channel_dim+1, *other_dims)}")
        x = x.permute(0, self.channel_dim+1, *other_dims) # [*, channel_dim, other_channel_0, other_channel_1]
        # We also need other_channel_0 and other_channel_1 to be the same size as img_size by padding them with 0s
        assert x.shape[-1] <= self.image_size, f'Last channel dimension {x.shape[-1]} is larger than image size {self.image_size}'
        assert x.shape[-2] <= self.image_size, f'Second to last channel dimension {x.shape[-2]} is larger than image size {self.image_size}'
        channel_1_pad = self.image_size - x.shape[-1]
        channel_0_pad = self.image_size - x.shape[-2]
        x = F.pad(x, (0, channel_1_pad, 0, channel_0_pad), mode='constant', value=0) # [*, channel_dim, img_size, img_size]
        # Now this is in the format for the visual transformer that was defined with input_channels = channel_dim
        x = super().forward(x)
        return x

class FlatTransformer(nn.Module):
    # Takes a flat input of shape [*, 4] where the last dimension is [t, x, y, z] and attends to a 1d conv of t with xyz converted to positional encodings
    def __init__(
        self,
        width: int, layers: int, heads: int, mlp_ratio: float,
        output_dim: int, act_layer: Callable = nn.GELU, dropout: float = 0.0
    ):
        super().__init__()
        self.width = width
        self.conv1 = nn.Conv1d(in_channels=1, out_channels=width, kernel_size=1, stride=1, bias=False)
        self.scale = width ** -0.5
        # self.positional_embedding = nn.Parameter(self.scale * torch.randn(sequence_len, width))
        self.positional_embedding = nn.Parameter(self.scale * torch.randn(width))

        self.ln_pre = LayerNorm(width)

        self.transformer = Transformer(width, layers, heads, mlp_ratio, act_layer=act_layer, dropout=dropout)

        self.ln_post = LayerNorm(width)
        self.proj = nn.Parameter(self.scale * torch.randn(width, output_dim))

    def generate_positional_encoding(self, x: torch.Tensor):
        p_x = x[..., 1]
        p_y = x[..., 2]
        p_z = x[..., 3]
        return self.positional_embedding.repeat(p_x.shape[0], 1)

    def forward(self, x: torch.Tensor):
        # Extract the dimensions to their own tensors
        logging.info(f"Input shape: {x.shape}")
        t = x[..., 0]
        logging.info(f"t shape: {t.shape}")
        e = self.conv1(t.unsqueeze(1)) # [*, width, sequence_len]
        e = e.permute(0, 2, 1) # [*, sequence_len, width]
        e = e + self.generate_positional_encoding(x)
        e = self.ln_pre(e)

        e = e.permute(1, 0, 2)  # NLD -> LND
        logging.info(f"Transformer input shape: {e.shape}")
        e = self.transformer(e)
        e = e.permute(1, 0, 2)  # LND -> NLD

        e = self.ln_post(e[:, 0, :])

        if self.proj is not None:
            logging.info(f"Shape before projection: {e.shape}. Projection shape: {self.proj.shape}")
            e = e @ self.proj
        
        logging.info(f"Output shape: {e.shape}")
        return e


        

# class Voxel3dTransformer(nn.Module):
#     # Very similar to the vision transformer, but using 3D convolutions instead of 2D convolutions
#     def __init__(
#         self, image_size: int, patch_size: int, width: int, layers: int, heads: int, mlp_ratio: float,
#             output_dim: int, act_layer: Callable = nn.GELU):

# class Voxel3dFlatTransformer(nn.Module):
#     def __init__(self, width: int, layers: int, heads: int, mlp_ratio: float, output_dim: int, act_layer: Callable = nn.GELU):
#         pass

class MLP(nn.Module):
    def __init__(self, input_dim: int, output_dim: int, hidden_dim: int, num_layers: int, act_layer: Callable = nn.GELU):
        super().__init__()
        input_layer = nn.Linear(input_dim, hidden_dim)
        output_layer = nn.Linear(hidden_dim, output_dim)
        layers = [
            ('input', input_layer)
        ]
        for i in range(num_layers - 1):
            layers.append(('fc{}'.format(i), nn.Linear(hidden_dim, hidden_dim)))
            layers.append(('gelu{}'.format(i + 2), act_layer()))
            layers.append(('dropout{}'.format(i + 2), nn.Dropout(0.3)))
            layers.append(('ln{}'.format(i + 2), LayerNorm(hidden_dim)))
        layers.append(('output', output_layer))
        self.layers = nn.Sequential(OrderedDict(layers))

    def forward(self, x: torch.Tensor):
        return self.layers(x)

class ClassVoxel3dConvEncoder(nn.Module):
    def __init__(self, dims: List[int], width: int, output_dim: int, act_layer: Callable = nn.GELU):
        super().__init__()
        self.channels = [64, 128, 256, 256, 256, 256, width]
        self.strides = [1, 1, 1, 2, 1, 2, 2]
        self.padding = [1, 1, 1, 1, 1, 1, 1]
        self.dialation = [1, 1, 1, 1, 1, 1, 1]
        self.kernel = [3, 3, 3, 3, 3, 3, 3]
        assert len(self.channels) == len(self.strides) == len(self.padding) == len(self.dialation) == len(self.kernel), f"Lengths of channels, strides, padding, dialation, and kernel must be the same. Got {len(self.channels)}, {len(self.strides)}, {len(self.padding)}, {len(self.dialation)}, {len(self.kernel)}"
        channels = [1] + self.channels
        self.conv_blocks = nn.ModuleList([
            self._get_conv_layer(channels[i], channels[i + 1], kernel_size=self.kernel[i], stride=self.strides[i], padding=self.padding[i], dilation=self.dialation[i], act_layer=act_layer)
            for i in range(len(self.channels))
        ])
        for n in range(len(self.channels)):
            stride = self.strides[n]
            dialation = self.dialation[n]
            padding = self.padding[n]
            kernel = self.kernel[n]
            dims = [int((d + 2*padding - dialation*(kernel - 1) - 1)/(stride) + 1) for d in dims]
            logging.info(f"Conv {n} output shape: {dims}")
        logging.info(f"Transformer sequence length: {np.prod(dims)} + 1. Transformer width: {width}")
        self.class_embedding = nn.Parameter(width**-0.5 * torch.randn(width))
        self.ln_pre = LayerNorm(width)
        self.transformer = Transformer(width, layers=4, heads=16, mlp_ratio=4, act_layer=act_layer, dropout=0.0)
        self.ln_post = LayerNorm(width)
        self.proj = nn.Parameter(width**-0.5 * torch.randn(width, output_dim))

    def _get_conv_layer(self, c_in, c_out, kernel_size, stride, padding, dilation, act_layer):
        return nn.Sequential(
            nn.Conv3d(c_in, c_out, kernel_size=kernel_size, stride=stride, padding=padding, dilation=dilation),
            nn.Dropout3d(0.1),
            act_layer(),
            # nn.MaxPool3d(kernel_size=2, stride=2)
        )

    def forward(self, x: torch.Tensor):
        for block in self.conv_blocks:
            x = block(x)
        # Current shape: [*, output_dim, x, y, z]
        x = x.reshape(x.shape[0], x.shape[1], -1) # [*, width, seq_len]
        x = x.permute(0, 2, 1) # [*, seq_len, width]
        # Add the class embedding
        x = torch.concat([
            self.class_embedding.to(x.dtype) + torch.zeros(x.shape[0], 1, x.shape[-1], dtype=x.dtype, device=x.device),
            x
        ], dim=1) # [*, 1 + seq_len, width]
        x = self.ln_pre(x)
        x = x.permute(1, 0, 2) # [1 + seq_len, *, width]
        x = self.transformer(x)
        x = x.permute(1, 0, 2) # [*, 1 + seq_len, width]
        x = self.ln_post(x[:, 0, :]) # [*, width]
        x = x @ self.proj # [*, output_dim]
        return x

class NewVoxel3dConvEncoder(nn.Module):
    def __init__(self, dims: List[int], attention_width: int, output_dim: int, c_in: int = 1, average_output: bool = False, act_layer: Callable = nn.GELU):
        super().__init__()
        self.average_output = average_output  # Average the output of the transformer instead of using a flattened linear layer
        self.channels = [64, 128, 256, 256, 256, attention_width]
        self.strides = [1, 1, 1, 2, 2, 2]
        self.padding = [1, 1, 1, 0, 1, 0]
        self.dialation = [1, 1, 1, 1, 1, 1]
        self.kernel = [3, 3, 3, 3, 3, 3]
        assert len(self.channels) == len(self.strides) == len(self.padding) == len(self.dialation) == len(self.kernel), f"Lengths of channels, strides, padding, dialation, and kernel must be the same. Got {len(self.channels)}, {len(self.strides)}, {len(self.padding)}, {len(self.dialation)}, {len(self.kernel)}"
        channels = [c_in] + self.channels
        self.conv_blocks = nn.ModuleList([
            self._get_conv_layer(channels[i], channels[i + 1], kernel_size=self.kernel[i], stride=self.strides[i], padding=self.padding[i], dilation=self.dialation[i], act_layer=act_layer)
            for i in range(len(self.channels))
        ])
        for n in range(len(self.channels)):
            stride = self.strides[n]
            dialation = self.dialation[n]
            padding = self.padding[n]
            kernel = self.kernel[n]
            dims = [int((d + 2*padding - dialation*(kernel - 1) - 1)/(stride) + 1) for d in dims]
            logging.info(f"Conv {n} output shape: {dims}")
        logging.info(f"Transformer sequence length: {np.prod(dims)}. Transformer width: {attention_width}")
        self.transformer = Transformer(attention_width, layers=2, heads=8, mlp_ratio=4, act_layer=act_layer, dropout=0.0)
        logging.info(f"Projection input features: {attention_width * dims[0] * dims[1] * dims[2]}")
        if self.average_output:
            self.proj = nn.Parameter(attention_width**-0.5 * torch.randn(attention_width, output_dim))
        else:
            self.proj = nn.Linear(attention_width * dims[0] * dims[1] * dims[2], output_dim)

    def _get_conv_layer(self, c_in, c_out, kernel_size, stride, padding, dilation, act_layer):
        return nn.Sequential(
            nn.Conv3d(c_in, c_out, kernel_size=kernel_size, stride=stride, padding=padding, dilation=dilation),
            nn.Dropout3d(0.1),
            act_layer(),
            # nn.MaxPool3d(kernel_size=2, stride=2)
        )

    def forward(self, x: torch.Tensor):
        for block in self.conv_blocks:
            x = block(x)
        # Currently the output shape is [*, attention_width, x, y, z]
        x = x.reshape(x.shape[0], x.shape[1], -1) # [*, attention_width, seq_len]
        x = x.permute(2, 0, 1) # [seq_len, *, attention_width]
        x = self.transformer(x)
        x = x.permute(1, 0, 2) # [*, seq_len, attention_width]
        if self.average_output:
            x = x.mean(dim=1)
            x = x @ self.proj
        else:
            x = x.reshape(x.shape[0], -1) # [*, attention_width * seq_len]
            x = self.proj(x)
        return x

class EmbeddingVoxel3dConvEncoder(NewVoxel3dConvEncoder):
    # Same as NewVoxel3dConvEncoder but with an embedding layer to take the value at [*, w, h, d] and blow it up into [*, w, h, d, c_in]
    def __init__(self, vocab_size: int, dims: List[int], attention_width: int, output_dim: int, c_in: int = 64, average_output: bool = False, act_layer: Callable = nn.GELU):
        super().__init__(dims, attention_width, output_dim, c_in, average_output, act_layer)
        self.token_embedding = nn.Embedding(vocab_size, c_in)

    def forward(self, x: torch.Tensor):
        # x: [*, w, h, d]
        x = self.token_embedding(x) # [*, w, h, d, c_in]
        return super().forward(x)

# class Voxel3dConvEncoder(nn.Module):
#     def __init__(self, dims: List[int], layers: int, output_dim: int, act_layer: Callable = nn.GELU):
#         super().__init__()
#         channels = [1, 64, 64, 64, 64, 64, 32, 32, 32, 32, 32, 32, 32]
#         strides = [1, 1, 1, 1, 1, 1, 2, 1, 1, 1, 1, 2]
#         self.conv_blocks = nn.ModuleList([
#             self._get_conv_layer(channels[i], channels[i + 1], kernel_size=3, stride=strides[i], act_layer=act_layer)
#             for i in range(layers)
#         ])
#         for n in range(layers):
#             stride = strides[n]
#             dialation = 1
#             padding = 0
#             kernel = 3
#             # (d + 2*padding - dialation*(kernel - 1) - 1)/(stride) + 1
#             dims = [int((d + 2*padding - dialation*(kernel - 1) - 1)/(stride) + 1) for d in dims]
#         logging.info(f"Expected output shape: {dims}")
#         self.mlp = MLP(channels[layers] * dims[0] * dims[1] * dims[2], output_dim, hidden_dim=output_dim*4, num_layers=3)
        
#     def _get_conv_layer(self, c_in, c_out, kernel_size, stride, act_layer):
#         return nn.Sequential(
#             nn.Conv3d(c_in, c_out, kernel_size=kernel_size, stride=stride),
#             nn.Dropout3d(0.1),
#             act_layer(),
#             # nn.MaxPool3d(kernel_size=2, stride=2)
#         )

#     def forward(self, x: torch.Tensor):
#         for block in self.conv_blocks:
#             x = block(x)
#         # logging.info(f"Actual output shape: {x.shape}")
#         x = x.view(x.shape[0], -1)
#         return self.mlp(x)

class Voxel3dConvEncoder(nn.Module):
    def __init__(self, dims: List[int], layers: int, output_dim: int, act_layer: Callable = nn.GELU):
        super().__init__()
        channels = [1, 32, 64, 64, 32, 32, 32, 32, 32]
        strides = [1, 1, 2, 1, 1, 1, 2, 1]
        self.conv_blocks = nn.ModuleList([
            self._get_conv_layer(channels[i], channels[i + 1], kernel_size=3, stride=strides[i], act_layer=act_layer)
            for i in range(layers)
        ])
        for n in range(layers):
            stride = strides[n]
            dialation = 1
            padding = 0
            kernel = 3
            # (d + 2*padding - dialation*(kernel - 1) - 1)/(stride) + 1
            dims = [int((d + 2*padding - dialation*(kernel - 1) - 1)/(stride) + 1) for d in dims]
        logging.info(f"Expected output shape: {dims}")
        self.mlp = MLP(channels[layers] * dims[0] * dims[1] * dims[2], output_dim, hidden_dim=output_dim*4, num_layers=3)
        
    def _get_conv_layer(self, c_in, c_out, kernel_size, stride, act_layer):
        return nn.Sequential(
            nn.Conv3d(c_in, c_out, kernel_size=kernel_size, stride=stride),
            nn.Dropout3d(0.1),
            act_layer(),
            # nn.MaxPool3d(kernel_size=2, stride=2)
        )

    def forward(self, x: torch.Tensor):
        for block in self.conv_blocks:
            x = block(x)
        # logging.info(f"Actual output shape: {x.shape}")
        x = x.view(x.shape[0], -1)
        return self.mlp(x)


@dataclass
class CLIPVisionCfg:
    layers: Union[Tuple[int, int, int, int], int] = 12
    width: int = 768
    head_width: int = 64
    mlp_ratio: float = 4.0
    patch_size: int = 16
    image_size: Union[Tuple[int, int], int] = 224
    timm_model_name: str = None  # a valid model name overrides layers, width, patch_size
    timm_model_pretrained: bool = False  # use (imagenet) pretrained weights for named model
    timm_pool: str = 'avg'  # feature pooling for timm model ('abs_attn', 'rot_attn', 'avg', '')
    timm_proj: str = 'linear'  # linear projection for timm model output ('linear', 'mlp', '')


@dataclass
class CLIPTextCfg:
    context_length: int = 77
    vocab_size: int = 49408
    width: int = 512
    heads: int = 8
    layers: int = 12

@dataclass 
class CLIPMlpVoxelCfg:
    voxel_dim: int = 15756
    layers: int = 1
    layer_width: int = 512

@dataclass
class CLIP3dConvNetCfg:
    dims: List[int] = None
    layers: int = 1

@dataclass
class CLIPVoxelTransformerCfg:
    width: int = 768
    layers: int = 12
    heads: int = 8

@dataclass
class CLIPVoxelVisualTransformerCfg:
    channel_dim: int = 2
    channels: int = 61
    image_size: int = 46
    layers: int = 8
    width: int = 2048
    head_width: int = 12
    mlp_ratio: float = 4.3637
    patch_size: int = 3

@dataclass
class CLIPVoxelCfg:
    config_mlp: CLIPMlpVoxelCfg = None
    config_2d_visual_transformer: CLIPVoxelVisualTransformerCfg = None
    config_3d_conv: CLIP3dConvNetCfg = None
    config_3d_transformer: CLIPVoxelTransformerCfg = None
    


class VoxelCLIP(nn.Module):
    def __init__(
        self,
        embed_dim: int,
        vision_cfg: CLIPVisionCfg,
        voxel_cfg: CLIPVoxelCfg,
        quick_gelu: bool = False,
        voxel_type: str = "flat",
        **kwargs
    ):
        super().__init__()
        if isinstance(vision_cfg, dict):
            vision_cfg = CLIPVisionCfg(**vision_cfg)
        if isinstance(voxel_cfg, dict):
            voxel_cfg = CLIPVoxelCfg(**voxel_cfg)

        # OpenAI models are pretrained w/ QuickGELU but native nn.GELU is both faster and more
        # memory efficient in recent PyTorch releases (>= 1.10).
        # NOTE: timm models always use native GELU regardless of quick_gelu flag.
        act_layer = QuickGELU if quick_gelu else nn.GELU

        if vision_cfg.timm_model_name:
            self.visual = TimmModel(
                vision_cfg.timm_model_name,
                pretrained=vision_cfg.timm_model_pretrained,
                pool=vision_cfg.timm_pool,
                proj=vision_cfg.timm_proj,
                embed_dim=embed_dim,
                image_size=vision_cfg.image_size
            )
            act_layer = nn.GELU  # so that text transformer doesn't use QuickGELU w/ timm models
        elif isinstance(vision_cfg.layers, (tuple, list)):
            vision_heads = vision_cfg.width * 32 // vision_cfg.head_width
            self.visual = ModifiedResNet(
                layers=vision_cfg.layers,
                output_dim=embed_dim,
                heads=vision_heads,
                image_size=vision_cfg.image_size,
                width=vision_cfg.width
            )
        else:
            vision_heads = vision_cfg.width // vision_cfg.head_width
            self.visual = VisualTransformer(
                image_size=vision_cfg.image_size,
                patch_size=vision_cfg.patch_size,
                width=vision_cfg.width,
                layers=vision_cfg.layers,
                heads=vision_heads,
                mlp_ratio=vision_cfg.mlp_ratio,
                output_dim=embed_dim,
                act_layer=act_layer,
            )

        logging.info(f"Generating model with voxel type {voxel_type}")
        self.voxel_type = voxel_type
        if voxel_type == "mlp":
            assert voxel_cfg.config_mlp is not None, "config_mlp is required for voxel_type=flat"
            # Basic MLP to test if training is working. Shouldn't overfit at least :p
            self.voxel_encoder = MLP(
                input_dim=voxel_cfg.config_mlp["voxel_dim"],
                output_dim=embed_dim,
                hidden_dim=voxel_cfg.config_mlp["layer_width"],
                num_layers=voxel_cfg.config_mlp["layers"],
                act_layer=act_layer,
            )
        elif voxel_type == "3d-conv":
            assert voxel_cfg.config_3d_conv is not None, "config_3d_conv is required for voxel_type=3d"
            # Uses a 3D convolutional network to encode voxel data.
            # raise NotImplementedError("3D voxel encoder not implemented")
            # self.voxel_encoder = Voxel3dConvEncoder(
            #     dims=voxel_cfg.config_3d_conv["dims"],
            #     layers=voxel_cfg.config_3d_conv["layers"],
            #     output_dim=embed_dim,
            #     act_layer=act_layer,
            # )
            self.voxel_encoder = NewVoxel3dConvEncoder(
                dims=voxel_cfg.config_3d_conv["dims"],
                attention_width=64,
                output_dim=embed_dim,
                average_output=False,
                act_layer=act_layer
            )
            # self.voxel_encoder = ClassVoxel3dConvEncoder(
            #     dims=voxel_cfg.config_3d_conv["dims"],
            #     width = 512,
            #     output_dim=embed_dim,
            #     act_layer=act_layer
            # )
        elif voxel_type == "embedding-3d-conv":
            assert voxel_cfg.config_3d_conv is not None, "config_3d_conv is required for voxel_type=3d"
            # This is exactly the same as 3d-conv, except that instead of expecting an input of [*, w, h, d, c_in], we expect [*, w, h, d] with an integer class as the value
            # We then have one pre-processing step before passing to the 3d-conv where we use an nn.Embedding layer to convert the integer class to a vector of size c_in
            self.voxel_encoder = EmbeddingVoxel3dConvEncoder(
                dims=voxel_cfg.config_3d_conv["dims"],
                attention_width=64,
                c_in=64,
                vocab_size=voxel_cfg.config_3d_conv["vocab_size"],
                output_dim=embed_dim,
                act_layer=act_layer
            )
        elif voxel_type == "3d-vision-transformer": 
            # Uses the visual transformer with a large channel dimension to encode voxel data.
            assert voxel_cfg.config_2d_visual_transformer is not None, "config_2d_visual_transformer is required for voxel_type=3d"
            vision_heads = voxel_cfg.config_2d_visual_transformer["width"] // voxel_cfg.config_2d_visual_transformer["head_width"]
            self.voxel_encoder = Voxel2DVisualTransformer(
                image_size=voxel_cfg.config_2d_visual_transformer["image_size"],
                patch_size=voxel_cfg.config_2d_visual_transformer["patch_size"],
                width=voxel_cfg.config_2d_visual_transformer["width"],
                layers=voxel_cfg.config_2d_visual_transformer["layers"],
                heads=vision_heads,
                mlp_ratio=voxel_cfg.config_2d_visual_transformer["mlp_ratio"],
                output_dim=embed_dim,
                act_layer=act_layer,
                input_channels=voxel_cfg.config_2d_visual_transformer["channels"],
                channel_dim=voxel_cfg.config_2d_visual_transformer["channel_dim"],
                dropout=0.2
            )
        elif voxel_type == "3d-transformer":
            assert voxel_cfg.config_3d_transformer is not None, "config_3d_transformer is required for voxel_type=3d"
            # Uses a transformer to encode voxel data.
            # raise NotImplementedError("3D flat voxel encoder not implemented")
            self.voxel_encoder = VisualTransformer3d(
                space_size=[42, 46, 61],
                patch_size=8,
                width=1408,
                layers=14,
                heads=8,
                mlp_ratio=4.3637,
                output_dim=embed_dim,
                act_layer=act_layer,
                dropout=0.4
            )
        elif voxel_type == "flat-transformer":
            # assert voxel_cfg.config_flat_transformer is not None, "config_flat_transformer is required for voxel_type=flat-transformer"
            self.voxel_encoder = FlatTransformer(
                width=512,
                layers=4,
                heads=8,
                mlp_ratio=4,
                output_dim=embed_dim,
                act_layer=act_layer,
                dropout=0.4
            )
        else:
            raise ValueError("Invalid voxel type: {}".format(voxel_type))

        self.logit_scale = nn.Parameter(torch.ones([]) * np.log(1 / 0.07))

        self.init_parameters()

    def init_parameters(self):
        nn.init.constant_(self.logit_scale, np.log(1 / 0.07))

        if hasattr(self.visual, 'init_parameters'):
            self.visual.init_parameters()

        if self.voxel_type == "flat":
            for fc_layer in self.voxel_encoder.layers:
                if isinstance(fc_layer, nn.Linear):
                    nn.init.normal_(fc_layer.weight, std=0.02)
                    nn.init.constant_(fc_layer.bias, 0)
        elif self.voxel_type == "3d":
            # raise NotImplementedError("3D voxel encoder initializer not implemented")
            pass
        elif self.voxel_type == "3d-flat":
            raise NotImplementedError("3D flat voxel encoder initializer not implemented")

    def lock_image_tower(self, unlocked_groups=0, freeze_bn_stats=False):
        # lock image tower as per LiT - https://arxiv.org/abs/2111.07991
        self.visual.lock(unlocked_groups=unlocked_groups, freeze_bn_stats=freeze_bn_stats)

    def encode_image(self, image):
        return self.visual(image)

    def encode_voxel(self, voxel):
        return self.voxel_encoder(voxel)

    def forward(self, image, text):
        if image is None:
            return self.encode_text(text)
        elif text is None:
            return self.encode_image(image)
        image_features = self.encode_image(image)
        image_features = F.normalize(image_features, dim=-1)

        text_features = self.encode_voxel(text)
        text_features = F.normalize(text_features, dim=-1)

        return image_features, text_features, self.logit_scale.exp()

class CLIP(nn.Module):
    def __init__(
            self,
            embed_dim: int,
            vision_cfg: CLIPVisionCfg,
            text_cfg: CLIPTextCfg,
            quick_gelu: bool = False,
    ):
        super().__init__()
        if isinstance(vision_cfg, dict):
            vision_cfg = CLIPVisionCfg(**vision_cfg)
        if isinstance(text_cfg, dict):
            text_cfg = CLIPTextCfg(**text_cfg)

        self.context_length = text_cfg.context_length

        # OpenAI models are pretrained w/ QuickGELU but native nn.GELU is both faster and more
        # memory efficient in recent PyTorch releases (>= 1.10).
        # NOTE: timm models always use native GELU regardless of quick_gelu flag.
        act_layer = QuickGELU if quick_gelu else nn.GELU

        if vision_cfg.timm_model_name:
            self.visual = TimmModel(
                vision_cfg.timm_model_name,
                pretrained=vision_cfg.timm_model_pretrained,
                pool=vision_cfg.timm_pool,
                proj=vision_cfg.timm_proj,
                embed_dim=embed_dim,
                image_size=vision_cfg.image_size
            )
            act_layer = nn.GELU  # so that text transformer doesn't use QuickGELU w/ timm models
        elif isinstance(vision_cfg.layers, (tuple, list)):
            vision_heads = vision_cfg.width * 32 // vision_cfg.head_width
            self.visual = ModifiedResNet(
                layers=vision_cfg.layers,
                output_dim=embed_dim,
                heads=vision_heads,
                image_size=vision_cfg.image_size,
                width=vision_cfg.width
            )
        else:
            vision_heads = vision_cfg.width // vision_cfg.head_width
            self.visual = VisualTransformer(
                image_size=vision_cfg.image_size,
                patch_size=vision_cfg.patch_size,
                width=vision_cfg.width,
                layers=vision_cfg.layers,
                heads=vision_heads,
                mlp_ratio=vision_cfg.mlp_ratio,
                output_dim=embed_dim,
                act_layer=act_layer,
            )

        self.transformer = Transformer(
            width=text_cfg.width,
            layers=text_cfg.layers,
            heads=text_cfg.heads,
            act_layer=act_layer,
        )

        self.vocab_size = text_cfg.vocab_size
        self.token_embedding = nn.Embedding(text_cfg.vocab_size, text_cfg.width)
        self.positional_embedding = nn.Parameter(torch.empty(self.context_length, text_cfg.width))
        self.ln_final = LayerNorm(text_cfg.width)

        self.text_projection = nn.Parameter(torch.empty(text_cfg.width, embed_dim))
        self.logit_scale = nn.Parameter(torch.ones([]) * np.log(1 / 0.07))
        self.register_buffer('attn_mask', self.build_attention_mask(), persistent=False)

        self.init_parameters()

    def init_parameters(self):
        nn.init.normal_(self.token_embedding.weight, std=0.02)
        nn.init.normal_(self.positional_embedding, std=0.01)
        nn.init.constant_(self.logit_scale, np.log(1 / 0.07))

        if hasattr(self.visual, 'init_parameters'):
            self.visual.init_parameters()

        proj_std = (self.transformer.width ** -0.5) * ((2 * self.transformer.layers) ** -0.5)
        attn_std = self.transformer.width ** -0.5
        fc_std = (2 * self.transformer.width) ** -0.5
        for block in self.transformer.resblocks:
            nn.init.normal_(block.attn.in_proj_weight, std=attn_std)
            nn.init.normal_(block.attn.out_proj.weight, std=proj_std)
            nn.init.normal_(block.mlp.c_fc.weight, std=fc_std)
            nn.init.normal_(block.mlp.c_proj.weight, std=proj_std)

        if self.text_projection is not None:
            nn.init.normal_(self.text_projection, std=self.transformer.width ** -0.5)

    def build_attention_mask(self):
        # lazily create causal attention mask, with full attention between the vision tokens
        # pytorch uses additive attention mask; fill with -inf
        mask = torch.empty(self.context_length, self.context_length)
        mask.fill_(float("-inf"))
        mask.triu_(1)  # zero out the lower diagonal
        return mask

    def lock_image_tower(self, unlocked_groups=0, freeze_bn_stats=False):
        # lock image tower as per LiT - https://arxiv.org/abs/2111.07991
        self.visual.lock(unlocked_groups=unlocked_groups, freeze_bn_stats=freeze_bn_stats)

    @torch.jit.ignore
    def set_grad_checkpointing(self, enable=True):
        self.visual.set_grad_checkpointing(enable)
        self.transformer.grad_checkpointing = enable

    def encode_image(self, image):
        return self.visual(image)

    def encode_text(self, text):
        x = self.token_embedding(text)  # [batch_size, n_ctx, d_model]

        x = x + self.positional_embedding
        x = x.permute(1, 0, 2)  # NLD -> LND
        x = self.transformer(x, attn_mask=self.attn_mask)
        x = x.permute(1, 0, 2)  # LND -> NLD
        x = self.ln_final(x)

        # x.shape = [batch_size, n_ctx, transformer.width]
        # take features from the eot embedding (eot_token is the highest number in each sequence)
        x = x[torch.arange(x.shape[0]), text.argmax(dim=-1)] @ self.text_projection

        return x

    def forward(self, image, text):
        if image is None:
            return self.encode_text(text)
        elif text is None:
            return self.encode_image(image)
        image_features = self.encode_image(image)
        image_features = F.normalize(image_features, dim=-1)

        text_features = self.encode_text(text)
        text_features = F.normalize(text_features, dim=-1)

        return image_features, text_features, self.logit_scale.exp()


def convert_weights_to_fp16(model: nn.Module):
    """Convert applicable model parameters to fp16"""

    def _convert_weights_to_fp16(l):
        if isinstance(l, (nn.Conv1d, nn.Conv2d, nn.Linear)):
            l.weight.data = l.weight.data.half()
            if l.bias is not None:
                l.bias.data = l.bias.data.half()

        if isinstance(l, nn.MultiheadAttention):
            for attr in [*[f"{s}_proj_weight" for s in ["in", "q", "k", "v"]], "in_proj_bias", "bias_k", "bias_v"]:
                tensor = getattr(l, attr)
                if tensor is not None:
                    tensor.data = tensor.data.half()

        for name in ["text_projection", "proj"]:
            if hasattr(l, name):
                attr = getattr(l, name)
                if attr is not None:
                    attr.data = attr.data.half()

    model.apply(_convert_weights_to_fp16)


def build_model_from_openai_state_dict(state_dict: dict):
    vit = "visual.proj" in state_dict

    if vit:
        vision_width = state_dict["visual.conv1.weight"].shape[0]
        vision_layers = len(
            [k for k in state_dict.keys() if k.startswith("visual.") and k.endswith(".attn.in_proj_weight")])
        vision_patch_size = state_dict["visual.conv1.weight"].shape[-1]
        grid_size = round((state_dict["visual.positional_embedding"].shape[0] - 1) ** 0.5)
        image_size = vision_patch_size * grid_size
    else:
        counts: list = [
            len(set(k.split(".")[2] for k in state_dict if k.startswith(f"visual.layer{b}"))) for b in [1, 2, 3, 4]]
        vision_layers = tuple(counts)
        vision_width = state_dict["visual.layer1.0.conv1.weight"].shape[0]
        output_width = round((state_dict["visual.attnpool.positional_embedding"].shape[0] - 1) ** 0.5)
        vision_patch_size = None
        assert output_width ** 2 + 1 == state_dict["visual.attnpool.positional_embedding"].shape[0]
        image_size = output_width * 32

    embed_dim = state_dict["text_projection"].shape[1]
    context_length = state_dict["positional_embedding"].shape[0]
    vocab_size = state_dict["token_embedding.weight"].shape[0]
    transformer_width = state_dict["ln_final.weight"].shape[0]
    transformer_heads = transformer_width // 64
    transformer_layers = len(set(k.split(".")[2] for k in state_dict if k.startswith(f"transformer.resblocks")))

    vision_cfg = CLIPVisionCfg(
        layers=vision_layers,
        width=vision_width,
        patch_size=vision_patch_size,
        image_size=image_size,
    )
    text_cfg = CLIPTextCfg(
        context_length=context_length,
        vocab_size=vocab_size,
        width=transformer_width,
        heads=transformer_heads,
        layers=transformer_layers
    )
    model = CLIP(
        embed_dim,
        vision_cfg=vision_cfg,
        text_cfg=text_cfg,
        quick_gelu=True,  # OpenAI models were trained with QuickGELU
    )

    for key in ["input_resolution", "context_length", "vocab_size"]:
        state_dict.pop(key, None)

    convert_weights_to_fp16(model)
    model.load_state_dict(state_dict)
    return model.eval()


def trace_model(model, batch_size=256, device=torch.device('cpu')):
    model.eval()
    image_size = model.visual.image_size
    example_images = torch.ones((batch_size, 3, image_size, image_size), device=device)
    example_text = torch.zeros((batch_size, model.context_length), dtype=torch.int, device=device)
    model = torch.jit.trace_module(
        model,
        inputs=dict(
            forward=(example_images, example_text),
            encode_text=(example_text,),
            encode_image=(example_images,)
        ))
    model.visual.image_size = image_size
    return model


def resize_pos_embed(state_dict, model, interpolation: str = 'bicubic', seq_dim=1):
    # Rescale the grid of position embeddings when loading from state_dict
    old_pos_embed = state_dict.get('visual.positional_embedding', None)
    if old_pos_embed is None or not hasattr(model.visual, 'grid_size'):
        return
    grid_size = to_2tuple(model.visual.grid_size)
    extra_tokens = 1  # FIXME detect different token configs (ie no class token, or more)
    new_seq_len = grid_size[0] * grid_size[1] + extra_tokens
    if new_seq_len == old_pos_embed.shape[0]:
        return

    if extra_tokens:
        pos_emb_tok, pos_emb_img = old_pos_embed[:extra_tokens], old_pos_embed[extra_tokens:]
    else:
        pos_emb_tok, pos_emb_img = None, old_pos_embed
    old_grid_size = to_2tuple(int(math.sqrt(len(pos_emb_img))))

    logging.info('Resizing position embedding grid-size from %s to %s', old_grid_size, grid_size)
    pos_emb_img = pos_emb_img.reshape(1, old_grid_size[0], old_grid_size[1], -1).permute(0, 3, 1, 2)
    pos_emb_img = F.interpolate(
        pos_emb_img,
        size=grid_size,
        mode=interpolation,
        align_corners=True,
    )
    pos_emb_img = pos_emb_img.permute(0, 2, 3, 1).reshape(1, grid_size[0] * grid_size[1], -1)[0]
    if pos_emb_tok is not None:
        new_pos_embed = torch.cat([pos_emb_tok, pos_emb_img], dim=0)
    else:
        new_pos_embed = pos_emb_img
    state_dict['visual.positional_embedding'] = new_pos_embed
