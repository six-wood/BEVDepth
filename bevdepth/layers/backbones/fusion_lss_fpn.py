# Copyright (c) Megvii Inc. All rights reserved.
import torch
import torch.nn as nn
from mmdet.models.backbones.resnet import BasicBlock

try:
    from bevdepth.ops.voxel_pooling_inference import voxel_pooling_inference
    from bevdepth.ops.voxel_pooling_train import voxel_pooling_train
except ImportError:
    print('Import VoxelPooling fail.')

from .base_lss_fpn import ASPP, BaseLSSFPN, Mlp, SELayer

__all__ = ['FusionLSSFPN']


class DepthNet(nn.Module):  # 一层卷积网络回归深度

    def __init__(self, in_channels, mid_channels, context_channels,
                 depth_channels):
        super(DepthNet, self).__init__()
        # 效果是一倍降采样
        self.reduce_conv = nn.Sequential(
            nn.Conv2d(in_channels,
                      mid_channels,
                      kernel_size=3,
                      stride=1,
                      padding=1),  # 卷积
            nn.BatchNorm2d(mid_channels),  # 正则化
            nn.ReLU(inplace=True),  # 激活
        )
        self.context_conv = nn.Conv2d(mid_channels,
                                      context_channels,
                                      kernel_size=1,
                                      stride=1,
                                      padding=0)  # 起MLP作用的卷积网络，改变通道数，生成目标维度语义信息
        self.mlp = Mlp(1, mid_channels, mid_channels)  # MLP
        self.se = SELayer(mid_channels)  # NOTE: add camera-aware Attention
        self.depth_gt_conv = nn.Sequential(
            nn.Conv2d(1, mid_channels, kernel_size=1, stride=1),
            nn.ReLU(inplace=True),
            nn.Conv2d(mid_channels, mid_channels, kernel_size=1, stride=1),
        )  # 用于预测深度带有卷积、激活
        self.depth_conv = nn.Sequential(
            BasicBlock(mid_channels, mid_channels),
            BasicBlock(mid_channels, mid_channels),
            BasicBlock(mid_channels, mid_channels),
        )  # resnet 基础残差模块
        self.aspp = ASPP(mid_channels, mid_channels)
        self.depth_pred = nn.Conv2d(mid_channels,
                                    depth_channels,
                                    kernel_size=1,
                                    stride=1,
                                    padding=0)  # 生成目标通道数的深度结果

    def forward(self, x, mats_dict, lidar_depth, scale_depth_factor=1000.0):
        x = self.reduce_conv(x)  # 1倍将采样
        context = self.context_conv(x)  # 改变通道数 512->80
        inv_intrinsics = torch.inverse(mats_dict['intrin_mats'][:, 0:1, ...])
        pixel_size = torch.norm(torch.stack(
            [inv_intrinsics[..., 0, 0], inv_intrinsics[..., 1, 1]], dim=-1),
            dim=-1).reshape(-1, 1)
        aug_scale = torch.sqrt(mats_dict['ida_mats'][:, 0, :, 0, 0]**2 +
                               mats_dict['ida_mats'][:, 0, :, 0,
                                                     0]**2).reshape(-1, 1)
        scaled_pixel_size = pixel_size * scale_depth_factor / aug_scale
        x_se = self.mlp(scaled_pixel_size)[..., None, None]  # 根据内参矩阵，增强矩阵生成特征量
        x = self.se(x, x_se)  # 通道加权
        depth = self.depth_gt_conv(lidar_depth)  # 先升维
        depth = self.depth_conv(x + depth)  # 语义与深度融合
        depth = self.aspp(depth)  # 多尺度融合
        # 预测深度depth feature channel
        depth = self.depth_pred(depth)
        return torch.cat([depth, context], dim=1)


class FusionLSSFPN(BaseLSSFPN):

    def _configure_depth_net(self, depth_net_conf):
        return DepthNet(
            depth_net_conf['in_channels'],
            depth_net_conf['mid_channels'],
            self.output_channels,
            self.depth_channels,
        )

    def _forward_depth_net(self, feat, mats_dict, lidar_depth):
        return self.depth_net(feat, mats_dict, lidar_depth)

    def _forward_single_sweep(self,
                              sweep_index,
                              sweep_imgs,
                              mats_dict,
                              sweep_lidar_depth,
                              is_return_depth=False):
        """Forward function for single sweep.

        Args:
            sweep_index (int): Index of sweeps.
            sweep_imgs (Tensor): Input images.
            mats_dict (dict):
                sensor2ego_mats(Tensor): Transformation matrix from
                    camera to ego with shape of (B, num_sweeps,
                    num_cameras, 4, 4).
                intrin_mats(Tensor): Intrinsic matrix with shape
                    of (B, num_sweeps, num_cameras, 4, 4).
                ida_mats(Tensor): Transformation matrix for ida with
                    shape of (B, num_sweeps, num_cameras, 4, 4).
                sensor2sensor_mats(Tensor): Transformation matrix
                    from key frame camera to sweep frame camera with
                    shape of (B, num_sweeps, num_cameras, 4, 4).
                bda_mat(Tensor): Rotation matrix for bda with shape
                    of (B, 4, 4).
            sweep_lidar_depth (Tensor): Depth generated by lidar.
            is_return_depth (bool, optional): Whether to return depth.
                Default: False.

        Returns:
            Tensor: BEV feature map.
        """
        batch_size, num_sweeps, num_cams, num_channels, img_height, \
            img_width = sweep_imgs.shape
        # 提取特征, batchsize*sweep*camera*C*H*W
        img_feats = self.get_cam_feats(sweep_imgs)
        sweep_lidar_depth = sweep_lidar_depth.reshape(
            batch_size * num_cams, *sweep_lidar_depth.shape[2:])  # batchsize*camera*1*H*W
        source_features = img_feats[:, 0, ...]
        depth_feature = self._forward_depth_net(  # 根据sweep[0]预测深度
            source_features.reshape(batch_size * num_cams,
                                    source_features.shape[2],
                                    source_features.shape[3],
                                    source_features.shape[4]), mats_dict,
            sweep_lidar_depth)
        depth = depth_feature[:, :self.depth_channels].softmax(
            dim=1, dtype=depth_feature.dtype)  # 转换为概率
        geom_xyz = self.get_geometry(
            mats_dict['sensor2ego_mats'][:, sweep_index, ...],
            mats_dict['intrin_mats'][:, sweep_index, ...],
            mats_dict['ida_mats'][:, sweep_index, ...],
            mats_dict.get('bda_mat', None),
        )
        geom_xyz = ((geom_xyz - (self.voxel_coord - self.voxel_size / 2.0)) /
                    self.voxel_size).int()  # 化为网格坐标
        if self.training or self.use_da:
            img_feat_with_depth = depth.unsqueeze(
                1) * depth_feature[:, self.depth_channels:(
                    self.depth_channels + self.output_channels)].unsqueeze(2)  # 升维

            img_feat_with_depth = self._forward_voxel_net(
                img_feat_with_depth)  # 特征提取

            img_feat_with_depth = img_feat_with_depth.reshape(
                batch_size,
                num_cams,
                img_feat_with_depth.shape[1],
                img_feat_with_depth.shape[2],
                img_feat_with_depth.shape[3],
                img_feat_with_depth.shape[4],
            )  # batchsize*camera*C*D*H*W

            img_feat_with_depth = img_feat_with_depth.permute(
                0, 1, 3, 4, 5, 2)  # batchsize*camera*D*H*W*C

            feature_map = voxel_pooling_train(geom_xyz,
                                              img_feat_with_depth.contiguous(),
                                              self.voxel_num.cuda())  # 基于cuda的voxel_pooling_train batchsize*C*H*W
        else:
            feature_map = voxel_pooling_inference(
                geom_xyz, depth, depth_feature[:, self.depth_channels:(
                    self.depth_channels + self.output_channels)].contiguous(),
                self.voxel_num.cuda())
        if is_return_depth:
            return feature_map.contiguous(), depth.float()
        return feature_map.contiguous()  # 输出voxel_pooling结果

    def forward(self,
                sweep_imgs,
                mats_dict,
                lidar_depth,
                timestamps=None,
                is_return_depth=False):
        """Forward function.

        Args:
            sweep_imgs(Tensor): Input images with shape of (B, num_sweeps,
                num_cameras, 3, H, W).
            mats_dict(dict):
                sensor2ego_mats(Tensor): Transformation matrix from
                    camera to ego with shape of (B, num_sweeps,
                    num_cameras, 4, 4).
                intrin_mats(Tensor): Intrinsic matrix with shape
                    of (B, num_sweeps, num_cameras, 4, 4).
                ida_mats(Tensor): Transformation matrix for ida with
                    shape of (B, num_sweeps, num_cameras, 4, 4).
                sensor2sensor_mats(Tensor): Transformation matrix
                    from key frame camera to sweep frame camera with
                    shape of (B, num_sweeps, num_cameras, 4, 4).
                bda_mat(Tensor): Rotation matrix for bda with shape
                    of (B, 4, 4).
            lidar_depth (Tensor): Depth generated by lidar.
            timestamps(Tensor): Timestamp for all images with the shape of(B,
                num_sweeps, num_cameras).

        Return:
            Tensor: bev feature map.
        """
        batch_size, num_sweeps, num_cams, num_channels, img_height, \
            img_width = sweep_imgs.shape

        # 降采样lidar尺寸
        lidar_depth = self.get_downsampled_lidar_depth(lidar_depth)

        # forward single sweep
        key_frame_res = self._forward_single_sweep(
            0,
            sweep_imgs[:, 0:1, ...],
            mats_dict,
            lidar_depth[:, 0, ...],
            is_return_depth=is_return_depth)
        if num_sweeps == 1:
            return key_frame_res

        key_frame_feature = key_frame_res[
            0] if is_return_depth else key_frame_res

        ret_feature_list = [key_frame_feature]
        for sweep_index in range(1, num_sweeps):
            with torch.no_grad():
                feature_map = self._forward_single_sweep(
                    sweep_index,
                    sweep_imgs[:, sweep_index:sweep_index + 1, ...],
                    mats_dict,
                    lidar_depth[:, sweep_index, ...],
                    is_return_depth=False)
                ret_feature_list.append(feature_map)

        if is_return_depth:
            return torch.cat(ret_feature_list, 1), key_frame_res[1]
        else:
            return torch.cat(ret_feature_list, 1)

    def get_downsampled_lidar_depth(self, lidar_depth):
        batch_size, num_sweeps, num_cams, height, width = lidar_depth.shape
        # 对H、W分别进行降采样
        lidar_depth = lidar_depth.view(
            batch_size * num_sweeps * num_cams,  # 0
            height // self.downsample_factor,  # 1
            self.downsample_factor,  # 2
            width // self.downsample_factor,  # 3
            self.downsample_factor,  # 4
            1,  # 5
        )
        lidar_depth = lidar_depth.permute(
            0, 1, 3, 5, 2, 4).contiguous()  # 交换顺序，把H,W重新靠在一起
        lidar_depth = lidar_depth.view(
            -1, self.downsample_factor * self.downsample_factor)  # 相当于16*16放到一起
        gt_depths_tmp = torch.where(lidar_depth == 0.0, lidar_depth.max(),
                                    lidar_depth)
        lidar_depth = torch.min(gt_depths_tmp, dim=-1).values  # 最小化聚合
        lidar_depth = lidar_depth.view(batch_size, num_sweeps, num_cams, 1,
                                       height // self.downsample_factor,
                                       width // self.downsample_factor)  # 恢复成原来的格式
        lidar_depth = lidar_depth / self.d_bound[1]  # 三个维度上的什么东西
        return lidar_depth
