import numpy as np
import math
import torch
from torch import nn
from torch.nn import functional as F
from typing import Union, Type, List, Tuple

from dynamic_network_architectures.building_blocks.helper import get_matching_convtransp

from torch.nn.modules.conv import _ConvNd
from torch.nn.modules.dropout import _DropoutNd
from dynamic_network_architectures.building_blocks.helper import convert_conv_op_to_dim

from dynamic_network_architectures.building_blocks.helper import get_matching_instancenorm, convert_dim_to_conv_op
from dynamic_network_architectures.initialization.weight_init import init_last_bn_before_add_to_0
from mlpipeline.models.segmentation.swin_umamba.network_initialization import InitWeights_He
from mamba_ssm import Mamba
from dynamic_network_architectures.building_blocks.helper import maybe_convert_scalar_to_list, get_matching_pool_op
from torch.cuda.amp import autocast
from dynamic_network_architectures.building_blocks.residual import BasicBlockD
from mlpipeline.models.segmentation.swin_umamba.k1_modules_3d import VSSBlock


class UpsampleLayer(nn.Module):
    def __init__(
        self,
        conv_op,
        input_channels,
        output_channels,
        pool_op_kernel_size,
        mode="nearest",
    ):
        super().__init__()
        self.conv = conv_op(input_channels, output_channels, kernel_size=1)
        self.pool_op_kernel_size = pool_op_kernel_size
        self.mode = mode

    def forward(self, x):
        x = F.interpolate(x, scale_factor=self.pool_op_kernel_size, mode=self.mode)
        x = self.conv(x)
        return x


class MambaLayer(nn.Module):
    def __init__(
        self, dim,
        d_state=16, d_conv=4, expand=2, channel_token=False,
        use_conv=False,
        use_depth=False,
        d_depth_stride=1,
        d_depth_squeeze=1,
        d_depth_out=224,
        depth_mode="",
        conv_mode="",
    ):
        super().__init__()
        print(f"MambaLayer: dim: {dim} {channel_token}")
        self.dim = dim
        ## whether to use channel as tokens
        self.channel_token = channel_token
        if self.channel_token:
            self.norm = nn.LayerNorm(dim)
            self.mamba = Mamba(
                d_model=dim,
                d_state=d_state,
                d_conv=d_conv,
                expand=2,
            )
        else:
            d_conv = d_conv - 1
            self.mamba = VSSBlock(
                hidden_dim=dim,
                d_state=d_state,
                d_conv=d_conv,
                expand=expand,
                drop_path=0.1,
                use_conv=use_conv,
                use_depth=use_depth,
                mode=depth_mode,
                d_depth_stride=d_depth_stride,
                d_depth_squeeze=d_depth_squeeze,
                d_depth_out=d_depth_out,
                conv_mode=conv_mode,
            )
        return

    def forward_patch_token(self, x):
        if x.dtype == torch.float16:
            x = x.type(torch.float32)

        x = x.permute(0, 2, 3, 4, 1)
        x_mamba = self.mamba(x)
        out = x_mamba.permute(0, 4, 1, 2, 3)
        return out

    def forward_channel_token(self, x):
        B, n_tokens = x.shape[:2]
        d_model = x.shape[2:].numel()

        assert d_model == self.dim, f"d_model: {d_model}, self.dim: {self.dim}"
        img_dims = x.shape[2:]
        x_flat = x.flatten(2)
        assert x_flat.shape[2] == d_model, f"x_flat.shape[2]: {x_flat.shape[2]}, d_model: {d_model}"
        x_norm = self.norm(x_flat)
        x_mamba = self.mamba(x_norm)
        out = x_mamba.reshape(B, n_tokens, *img_dims)
        return out

    @autocast(enabled=False)
    def forward(self, x):
        if x.dtype == torch.float16 or x.dtype == torch.bfloat16:
            x = x.type(torch.float32)

        if self.channel_token:
            out = self.forward_channel_token(x)
        else:
            out = self.forward_patch_token(x)

        return out


class BasicResBlock(nn.Module):
    def __init__(
        self,
        conv_op,
        input_channels,
        output_channels,
        norm_op,
        norm_op_kwargs,
        kernel_size=3,
        padding=1,
        stride=1,
        use_1x1conv=False,
        nonlin=nn.LeakyReLU,
        nonlin_kwargs={"inplace": True},
    ):
        super().__init__()

        self.conv1 = conv_op(input_channels, output_channels, kernel_size, stride=stride, padding=padding)
        self.norm1 = norm_op(output_channels, **norm_op_kwargs)
        self.act1 = nonlin(**nonlin_kwargs)

        self.conv2 = conv_op(output_channels, output_channels, kernel_size, padding=padding)
        self.norm2 = norm_op(output_channels, **norm_op_kwargs)
        self.act2 = nonlin(**nonlin_kwargs)

        if use_1x1conv:
            self.conv3 = conv_op(input_channels, output_channels, kernel_size=1, stride=stride)
        else:
            self.conv3 = None

    def forward(self, x):
        y = self.conv1(x)
        y = self.act1(self.norm1(y))
        y = self.norm2(self.conv2(y))
        if self.conv3:
            x = self.conv3(x)
        y += x
        return self.act2(y)


class ResidualMambaEncoder(nn.Module):
    def __init__(
        self,
        input_size: Tuple[int, ...],
        input_channels: int,
        n_stages: int,
        features_per_stage: Union[int, List[int], Tuple[int, ...]],
        conv_op: Type[_ConvNd],
        kernel_sizes: Union[int, List[int], Tuple[int, ...]],
        strides: Union[int, List[int], Tuple[int, ...], Tuple[Tuple[int, ...], ...]],
        n_blocks_per_stage: Union[int, List[int], Tuple[int, ...]],
        depth_mode: str,
        conv_mode: str,
        expand: int,
        conv_bias: bool = False,
        norm_op: Union[None, Type[nn.Module]] = None,
        norm_op_kwargs: dict = None,
        nonlin: Union[None, Type[torch.nn.Module]] = None,
        nonlin_kwargs: dict = None,
        return_skips: bool = False,
        stem_channels: int = None,
        pool_type: str = "conv",
    ):
        super().__init__()
        if isinstance(kernel_sizes, int):
            kernel_sizes = [kernel_sizes] * n_stages
        if isinstance(features_per_stage, int):
            features_per_stage = [features_per_stage] * n_stages
        if isinstance(n_blocks_per_stage, int):
            n_blocks_per_stage = [n_blocks_per_stage] * n_stages
        if isinstance(strides, int):
            strides = [strides] * n_stages
        assert len(
            kernel_sizes) == n_stages, "kernel_sizes must have as many entries as we have resolution stages (n_stages)"
        assert len(
            n_blocks_per_stage) == n_stages, "n_conv_per_stage must have as many entries as we have resolution stages (n_stages)"
        assert len(
            features_per_stage) == n_stages, "features_per_stage must have as many entries as we have resolution stages (n_stages)"
        assert len(strides) == n_stages, "strides must have as many entries as we have resolution stages (n_stages). " \
            "Important: first entry is recommended to be 1, else we run strided conv drectly on the input"

        pool_op = get_matching_pool_op(conv_op, pool_type=pool_type) if pool_type != "conv" else None

        do_channel_token = [False] * n_stages
        feature_map_sizes = []
        feature_map_size = input_size
        for s in range(n_stages):
            feature_map_sizes.append([i // j for i, j in zip(feature_map_size, strides[s])])
            feature_map_size = feature_map_sizes[-1]
            if np.prod(feature_map_size) <= features_per_stage[s]:
                # do_channel_token[s] = True
                do_channel_token[s] = False

        print(f"feature_map_sizes: {feature_map_sizes}")
        print(f"do_channel_token: {do_channel_token}")

        use_convs = [True, True, True, False, False]
        use_depths = [False, True, True, True, True]
        d_depth_strides = [0, (4, 4, 4), (2, 2, 2), (1, 1, 1), (1, 1, 1), 0]
        d_depth_squeezes = [0, 1, 1, 2, 8, 0]
        d_depth_outs = [0, 512, 512, 512, 64, 0]

        self.conv_pad_sizes = []
        for krnl in kernel_sizes:
            self.conv_pad_sizes.append([i // 2 for i in krnl])

        stem_channels = features_per_stage[0]
        self.stem = nn.Sequential(
            BasicResBlock(
                conv_op = conv_op,
                input_channels = input_channels,
                output_channels = stem_channels,
                norm_op=norm_op,
                norm_op_kwargs=norm_op_kwargs,
                kernel_size=kernel_sizes[0],
                padding=self.conv_pad_sizes[0],
                stride=1,
                nonlin=nonlin,
                nonlin_kwargs=nonlin_kwargs,
                use_1x1conv=True,
            ),
            *[
                BasicBlockD(
                    conv_op = conv_op,
                    input_channels = stem_channels,
                    output_channels = stem_channels,
                    kernel_size = kernel_sizes[0],
                    stride = 1,
                    conv_bias = conv_bias,
                    norm_op = norm_op,
                    norm_op_kwargs = norm_op_kwargs,
                    nonlin = nonlin,
                    nonlin_kwargs = nonlin_kwargs,
                ) for _ in range(n_blocks_per_stage[0] - 1)
            ]
        )

        input_channels = stem_channels

        stages = []
        mamba_layers = []
        for s in range(n_stages):
            stage = nn.Sequential(
                BasicResBlock(
                    conv_op = conv_op,
                    norm_op = norm_op,
                    norm_op_kwargs = norm_op_kwargs,
                    input_channels = input_channels,
                    output_channels = features_per_stage[s],
                    kernel_size = kernel_sizes[s],
                    padding=self.conv_pad_sizes[s],
                    stride=strides[s],
                    use_1x1conv=True,
                    nonlin = nonlin,
                    nonlin_kwargs = nonlin_kwargs,
                ),
                *[
                    BasicBlockD(
                        conv_op = conv_op,
                        input_channels = features_per_stage[s],
                        output_channels = features_per_stage[s],
                        kernel_size = kernel_sizes[s],
                        stride = 1,
                        conv_bias = conv_bias,
                        norm_op = norm_op,
                        norm_op_kwargs = norm_op_kwargs,
                        nonlin = nonlin,
                        nonlin_kwargs = nonlin_kwargs,
                    ) for _ in range(n_blocks_per_stage[s] - 1)
                ]
            )

            mamba_layers.append(
                MambaLayer(
                    dim = np.prod(feature_map_sizes[s]) if do_channel_token[s] else features_per_stage[s],
                    channel_token=do_channel_token[s],
                    use_conv=use_convs[s],
                    use_depth=use_depths[s],
                    d_depth_stride=d_depth_strides[s],
                    d_depth_squeeze=d_depth_squeezes[s],
                    d_depth_out=d_depth_outs[s],
                    depth_mode=depth_mode,
                    conv_mode=conv_mode,
                    expand=expand,
                )
            )

            stages.append(stage)
            input_channels = features_per_stage[s]

        self.mamba_layers = nn.ModuleList(mamba_layers)
        self.stages = nn.ModuleList(stages)
        self.output_channels = features_per_stage
        self.strides = [maybe_convert_scalar_to_list(conv_op, i) for i in strides]
        self.return_skips = return_skips

        self.conv_op = conv_op
        self.norm_op = norm_op
        self.norm_op_kwargs = norm_op_kwargs
        self.nonlin = nonlin
        self.nonlin_kwargs = nonlin_kwargs
        #self.dropout_op = dropout_op
        #self.dropout_op_kwargs = dropout_op_kwargs
        self.conv_bias = conv_bias
        self.kernel_sizes = kernel_sizes

    def forward(self, x):
        if self.stem is not None:
            x = self.stem(x)

        stem = x
        x = F.max_pool3d(x, kernel_size=2, stride=2)

        ret = []
        for s in range(len(self.stages)):
            x = self.stages[s](x)
            x = self.mamba_layers[s](x)
            ret.append(x)
        if self.return_skips:
            return ret, stem
        return ret[-1], stem

    def compute_conv_feature_map_size(self, input_size):
        if self.stem is not None:
            output = self.stem.compute_conv_feature_map_size(input_size)
        else:
            output = np.int64(0)

        for s in range(len(self.stages)):
            output += self.stages[s].compute_conv_feature_map_size(input_size)
            input_size = [i // j for i, j in zip(input_size, self.strides[s])]

        return output


class UNetResDecoder(nn.Module):
    def __init__(self,
        encoder,
        num_classes,
        n_conv_per_stage: Union[int, Tuple[int, ...], List[int]],
        deep_supervision, nonlin_first: bool = False,
    ):
        super().__init__()
        self.deep_supervision = deep_supervision
        self.encoder = encoder
        self.num_classes = num_classes
        n_stages_encoder = len(encoder.output_channels)
        if isinstance(n_conv_per_stage, int):
            n_conv_per_stage = [n_conv_per_stage] * (n_stages_encoder - 1)
        assert len(n_conv_per_stage) == n_stages_encoder - 1, "n_conv_per_stage must have as many entries as we have " \
            "resolution stages - 1 (n_stages in encoder - 1), " \
            "here: %d" % n_stages_encoder

        stages = []
        upsample_layers = []

        for s in range(1, n_stages_encoder):
            input_features_below = encoder.output_channels[-s]
            input_features_skip = encoder.output_channels[-(s + 1)]
            stride_for_upsampling = encoder.strides[-s]

            upsample_layers.append(UpsampleLayer(
                conv_op = encoder.conv_op,
                input_channels = input_features_below,
                output_channels = input_features_skip,
                pool_op_kernel_size = stride_for_upsampling,
                mode="trilinear",
            ))

            stages.append(nn.Sequential(
                BasicResBlock(
                    conv_op = encoder.conv_op,
                    norm_op = encoder.norm_op,
                    norm_op_kwargs = encoder.norm_op_kwargs,
                    nonlin = encoder.nonlin,
                    nonlin_kwargs = encoder.nonlin_kwargs,
                    input_channels = input_features_skip,
                    output_channels = input_features_skip,
                    kernel_size = encoder.kernel_sizes[-(s + 1)],
                    padding=encoder.conv_pad_sizes[-(s + 1)],
                    stride=1,
                    use_1x1conv=True,
                ),
                *[
                    BasicBlockD(
                        conv_op = encoder.conv_op,
                        input_channels = input_features_skip,
                        output_channels = input_features_skip,
                        kernel_size = encoder.kernel_sizes[-(s + 1)],
                        stride = 1,
                        conv_bias = encoder.conv_bias,
                        norm_op = encoder.norm_op,
                        norm_op_kwargs = encoder.norm_op_kwargs,
                        nonlin = encoder.nonlin,
                        nonlin_kwargs = encoder.nonlin_kwargs,
                    ) for _ in range(n_conv_per_stage[s-1] - 1)
                ]
            ))

        self.stages = nn.ModuleList(stages)
        self.upsample_layers = nn.ModuleList(upsample_layers)

    def forward(self, skips):
        lres_input = skips[-1]
        x = None
        for s in range(len(self.stages)):
            x = self.upsample_layers[s](lres_input)
            x = x + skips[-(s+2)]
            x = self.stages[s](x)
            lres_input = x

        return x

    def compute_conv_feature_map_size(self, input_size):
        skip_sizes = []
        for s in range(len(self.encoder.strides) - 1):
            skip_sizes.append([i // j for i, j in zip(input_size, self.encoder.strides[s])])
            input_size = skip_sizes[-1]

        assert len(skip_sizes) == len(self.stages)

        output = np.int64(0)
        for s in range(len(self.stages)):
            output += self.stages[s].compute_conv_feature_map_size(skip_sizes[-(s+1)])
            output += np.prod([self.encoder.output_channels[-(s+2)], *skip_sizes[-(s+1)]], dtype=np.int64)
            if self.deep_supervision or (s == (len(self.stages) - 1)):
                output += np.prod([self.num_classes, *skip_sizes[-(s+1)]], dtype=np.int64)
        return output


class UMambaEnc(nn.Module):
    def __init__(
        self,
        input_size: Tuple[int, ...],
        input_channels: int,
        n_stages: int,
        features_per_stage: Union[int, List[int], Tuple[int, ...]],
        conv_op: Type[_ConvNd],
        kernel_sizes: Union[int, List[int], Tuple[int, ...]],
        strides: Union[int, List[int], Tuple[int, ...]],
        n_conv_per_stage: Union[int, List[int], Tuple[int, ...]],
        depth_mode: str,
        conv_mode: str,
        expand: int,
        num_classes: int,
        n_conv_per_stage_decoder: Union[int, Tuple[int, ...], List[int]],
        conv_bias: bool = False,
        norm_op: Union[None, Type[nn.Module]] = None,
        norm_op_kwargs: dict = None,
        dropout_op: Union[None, Type[_DropoutNd]] = None,
        dropout_op_kwargs: dict = None,
        nonlin: Union[None, Type[torch.nn.Module]] = None,
        nonlin_kwargs: dict = None,
        deep_supervision: bool = False,
        stem_channels: int = None,
    ):
        super().__init__()
        n_blocks_per_stage = n_conv_per_stage
        if isinstance(n_blocks_per_stage, int):
            n_blocks_per_stage = [n_blocks_per_stage] * n_stages
        if isinstance(n_conv_per_stage_decoder, int):
            n_conv_per_stage_decoder = [n_conv_per_stage_decoder] * (n_stages - 1)

        for s in range(math.ceil(n_stages / 2), n_stages):
            n_blocks_per_stage[s] = 1

        for s in range(math.ceil((n_stages - 1) / 2 + 0.5), n_stages - 1):
            n_conv_per_stage_decoder[s] = 1

        assert len(n_blocks_per_stage) == n_stages, "n_blocks_per_stage must have as many entries as we have " \
            f"resolution stages. here: {n_stages}. " \
            f"n_blocks_per_stage: {n_blocks_per_stage}"
        assert len(n_conv_per_stage_decoder) == (n_stages - 1), "n_conv_per_stage_decoder must have one less entries " \
            f"as we have resolution stages. here: {n_stages} " \
            f"stages, so it should have {n_stages - 1} entries. " \
            f"n_conv_per_stage_decoder: {n_conv_per_stage_decoder}"

        self.encoder = ResidualMambaEncoder(
            input_size,
            input_channels,
            n_stages,
            features_per_stage,
            conv_op,
            kernel_sizes,
            strides,
            n_blocks_per_stage,
            depth_mode,
            conv_mode,
            expand,
            conv_bias,
            norm_op,
            norm_op_kwargs,
            nonlin,
            nonlin_kwargs,
            return_skips=True,
            stem_channels=stem_channels,
        )
        self.decoder = UNetResDecoder(self.encoder, num_classes, n_conv_per_stage_decoder, deep_supervision)
        self.upsample = UpsampleLayer(
            conv_op=conv_op,
            input_channels=features_per_stage[0],
            output_channels=features_per_stage[0],
            pool_op_kernel_size=2,
            mode="trilinear",
        )
        self.conv = BasicResBlock(
            conv_op=conv_op,
            norm_op=norm_op,
            norm_op_kwargs=norm_op_kwargs,
            nonlin=nonlin,
            nonlin_kwargs=nonlin_kwargs,
            input_channels=features_per_stage[0],
            output_channels=features_per_stage[0],
            kernel_size=3,
            padding=1,
            stride=1,
        )
        self.seg_layer = conv_op(features_per_stage[0], num_classes, 1)

    def forward(self, x):
        skips, stem = self.encoder(x)
        decoder_out = self.decoder(skips)
        decoder_out = self.upsample(decoder_out)
        out = stem + decoder_out
        out = self.conv(out)
        out = self.seg_layer(out)
        return out

    def compute_conv_feature_map_size(self, input_size):
        assert len(input_size) == convert_conv_op_to_dim(self.encoder.conv_op), "just give the image size without color/feature channels or " \
            "batch channel. Do not give input_size=(b, c, x, y(, z)). " \
            "Give input_size=(x, y(, z))!"
        return self.encoder.compute_conv_feature_map_size(input_size) + self.decoder.compute_conv_feature_map_size(input_size)


def get_umamba_enc_dc_k1_3d_from_plans(
    num_input_channels: int,
    num_output_channels: int,
    depth_mode: str,
    conv_mode: str,
    expand: int,
    deep_supervision: bool = False,
):
    """
    we may have to change this in the future to accommodate other plans -> network mappings

    num_input_channels can differ depending on whether we do cascade. Its best to make this info available in the
    trainer rather than inferring it again from the plans here.
    """
    num_layers = 5
    conv_kernel_sizes = [[3, 3, 3]] * num_layers
    pool_op_kernel_sizes = [[1, 1, 1]] + [[2, 2, 2]] * (num_layers - 1)
    num_stages = len(conv_kernel_sizes)
    dim = len(conv_kernel_sizes[0])
    conv_op = convert_dim_to_conv_op(dim)

    segmentation_network_class_name = "UMambaEnc"
    network_class = UMambaEnc
    kwargs = {
        "UMambaEnc": {
            "input_size": (128, 128, 128),
            "conv_bias": True,
            "norm_op": get_matching_instancenorm(conv_op),
            "norm_op_kwargs": {"eps": 1e-5, "affine": True},
            "dropout_op": None, "dropout_op_kwargs": None,
            "nonlin": nn.LeakyReLU, "nonlin_kwargs": {"inplace": True},
        }
    }

    conv_or_blocks_per_stage = {
        "n_conv_per_stage": 2,
        "n_conv_per_stage_decoder": 2,
    }

    model = network_class(
        input_channels=num_input_channels,
        n_stages=num_stages,
        features_per_stage=[min(
            32 * 2 ** i,
            320) for i in range(num_stages)],
        conv_op=conv_op,
        kernel_sizes=conv_kernel_sizes,
        strides=pool_op_kernel_sizes,
        num_classes=num_output_channels,
        depth_mode=depth_mode,
        conv_mode=conv_mode,
        expand=expand,
        deep_supervision=deep_supervision,
        **conv_or_blocks_per_stage,
        **kwargs[segmentation_network_class_name],
    )
    model.apply(InitWeights_He(1e-2))

    return model
