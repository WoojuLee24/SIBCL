# -*- coding: utf-8 -*-
"""
Created on Fri Dec  4 11:44:19 2020

@author: loocy
"""

from pixloc.pixlib.datasets.base_dataset import BaseDataset
import numpy as np
import os
from PIL import Image
from torch.utils.data import DataLoader, Dataset
from torchvision import transforms
import torch
import pandas as pd
import pixloc.pixlib.datasets.Kitti_utils as Kitti_utils
import time
from matplotlib import pyplot as plt
from sklearn.neighbors import NearestNeighbors
import pixloc.pixlib.datasets.Kitti_gps_coord_func as gps_func
import h5py
import random
import cv2
from glob import glob
from pixloc.pixlib.datasets.transformations import euler_from_matrix, euler_matrix
from pixloc.pixlib.geometry import Camera, Pose

visualise_debug = True

# depth_map_dir = '../../../depth/GANet/Kitti/'
# keypoints_dir = 'Keypoints/lidar_512_points.h5' #lidar_64_points.h5 #lidar_orb_2_32_points.h5 #lidar_512_points.h5 # more data does not help
root_dir = '/students/u6617221/shan/data'
test_csv_file_name = 'test.csv'
ignore_csv_file_name = 'ignore.csv'
satmap_dir = 'satmap_20'
grdimage_dir = 'raw_data'
left_color_camera_dir = 'image_02/data'
right_color_camera_dir = 'image_03/data'
oxts_dir = 'oxts/data'
vel_dir = 'velodyne_points/data'

#grd_ori_size = (375, 1242) # different size in Kitti 375×1242, 370×1224,374×1238, and376×1241
grd_pad_size = (384, 1248)
satellite_ori_size = 1280


ToTensor = transforms.Compose([
    transforms.ToTensor()])

class Kitti(BaseDataset):
    default_conf = {
        'dataset_dir': '/data/Kitti',

        'two_view': True,
        'min_overlap': 0.3,
        'max_overlap': 1.,
        'min_baseline': None,
        'max_baseline': None,
        'sort_by_overlap': False,

        'grayscale': False,
        'resize': None,
        'resize_by': 'max',
        'crop': None,
        'pad': None,
        'optimal_crop': True,
        'seed': 0,

        'max_num_points3D': 15000,
        'force_num_points3D': False,
    }

    def _init(self, conf):
        pass

    def get_dataset(self, split):
        assert split != 'test', 'Not supported'
        return _Dataset(self.conf, split)

def read_calib(calib_file_name):
    with open(calib_file_name, 'r') as f:
        lines = f.readlines()
        for line in lines:
            # left color camera k matrix
            if 'P_rect_02' in line:
                # get 3*3 matrix from P_rect_**:
                items = line.split(':')
                valus = items[1].strip().split(' ')

                camera_P = np.asarray([float(i) for i in valus]).reshape((3, 4))
                fx = float(valus[0])
                cx = float(valus[2])
                fy = float(valus[5])
                cy = float(valus[6])

                camera_k = [[fx, 0, cx], [0, fy, cy], [0, 0, 1]]
                camera_k = np.asarray(camera_k)
                tx, ty, tz = float(valus[3]), float(valus[7]), float(valus[11])
                camera_t = np.linalg.inv(camera_k) @ np.asarray([tx, ty, tz])

            if 'R_rect_02' in line:
                items = line.split(':')
                valus = items[1].strip().split(' ')
                camera_R = np.asarray([float(i) for i in valus]).reshape((3, 3))

    return camera_k, camera_R, camera_t, camera_P


def read_sensor_rel_pose(file_name):
    with open(file_name, 'r') as f:
        lines = f.readlines()
        for line in lines:
            # left color camera k matrix
            if 'R' in line:
                # get 3*3 matrix from P_rect_**:
                items = line.split(':')
                valus = items[1].strip().split(' ')
                R = np.asarray([float(i) for i in valus]).reshape((3, 3))

            if 'T' in line:
                # get 3*3 matrix from P_rect_**:
                items = line.split(':')
                valus = items[1].strip().split(' ')
                T = np.asarray([float(i) for i in valus]).reshape((3))
    rel_pose_hom = np.eye(4)
    rel_pose_hom[:3, :3] = R
    rel_pose_hom[:3, 3] = T

    return R, T, rel_pose_hom



def project_lidar_to_cam(velodyne, camera_P, camera_k, camera_R, camera_t, lidar2cam_R, lidar2cam_T, img_size):
    # lidar2cam_R2 = np.array([[0, -1, 0], [0, 0, -1], [1, 0, 0]])
    # translation2 = np.array([ 0.06, 0, -0.27])
    # points3d2 = lidar2cam_R2 @ velodyne.T[:3,:] + translation2[:,None]
    # img_size (H,W)

    points3d = lidar2cam_R @ velodyne.T[:3, :] + lidar2cam_T[:, None]
    points3d = camera_R @ points3d + camera_t[:, None]
    # # ignore out of satellite range 0~64m
    # velo_mask = (points3d[2,:] > 0) & (points3d[2,:] < 31)
    velo_mask = points3d[2, :] > 0
    points3d = points3d[:, velo_mask]
    velodyne_trimed = velodyne.T[:, velo_mask]

    # project the points to the camera
    points2d = camera_k @ points3d
    points2d = points2d[:2, :] / points2d[2, :]

    u_mask = (points2d[0] < img_size[1]) & (points2d[0] > 0)
    v_mask = (points2d[1] < img_size[0]) & (points2d[1] > 0)
    uv_mask = u_mask & v_mask
    return points2d[:, uv_mask], points3d[:, uv_mask], velodyne_trimed[:, uv_mask]

def project_lidar_to_sat(velodyne_imu, imu_rot, meter_per_pixel, body_location):
    # imu to sat
    imu2sat = np.eye(4)
    imu2sat[1,1] = -1 # y=-y
    imu2sat[2,2] = -1 # z=-z
    imu2sat = imu2sat@imu_rot
    velodyne_sat_3d = imu2sat @ velodyne_imu
    #velodyne_sat_3d[2,:] = np.ones_like(velodyne_sat_3d[2,:] )

    # convert the velodyne_world to pixels, with respect to the GPS/IMU pixel location
    velodyne_sat_2d = velodyne_sat_3d[:2, :] / meter_per_pixel
    #velodyne_world_pixels[1, :] = - velodyne_world_pixels[1, :]
    velodyne_sat_2d = velodyne_sat_2d + np.asarray(body_location)[:, None]
    # visible mask
    u_mask = (velodyne_sat_2d[0] < satellite_ori_size) & (velodyne_sat_2d[0] > 0)
    v_mask = (velodyne_sat_2d[1] < satellite_ori_size) & (velodyne_sat_2d[1] > 0)
    sat_mask = u_mask & v_mask
    # matching 3D locations
    velodyne_sat_2d = velodyne_sat_2d[:, sat_mask].T
    velodyne_sat_3d = velodyne_sat_3d[:3, sat_mask].T
    velodyne_imu = velodyne_imu[:3, sat_mask].T

    return velodyne_sat_2d, velodyne_sat_3d, velodyne_imu, sat_mask, imu2sat

class _Dataset(Dataset):
    def __init__(self, conf, split):
        self.root = root_dir
        self.conf = conf

        # for keypoints load
        # self.keypoints = h5py.File(os.path.join(root,keypoints_dir), "r") #scio.loadmat(os.path.join(root, keypoints_dir))

        # for sat embedding
        # SatMap_size = utils.get_process_satmap_edge()
        # y, x = torch.meshgrid(torch.arange(SatMap_size), torch.arange(SatMap_size))
        # y = y * 2 / SatMap_size - 1
        # x = x * 2 / SatMap_size - 1
        # x_no0 = torch.where(abs(x) < 1E-6, torch.tensor(1E-6), x)  # /0 process
        # self.sat_yaw = ((torch.arctan(-y / x_no0) % (2.0 * np.pi)) / np.pi).unsqueeze(0)-1
        # self.sat_dis = torch.sqrt(torch.pow(x, 2) + torch.pow(y, 2)).unsqueeze(0)

        # satmap NED coords
        file = os.path.join(self.root, grdimage_dir, 'satellite_gps_center.npy')
        self.Geodetic = np.load(file)
        NED_coords_satellite = np.zeros((self.Geodetic.shape[0], 3))
        for i in range(self.Geodetic.shape[0]):
            x, y, z = gps_func.GeodeticToEcef(self.Geodetic[i, 0] * np.pi / 180.0, self.Geodetic[i, 1] * np.pi / 180.0,
                                              self.Geodetic[i, 2])
            xEast, yNorth, zUp = gps_func.EcefToEnu(x, y, z, self.Geodetic[0, 0] * np.pi / 180.0,
                                                    self.Geodetic[0, 1] * np.pi / 180.0, self.Geodetic[0, 2])
            NED_coords_satellite[i, 0] = xEast
            NED_coords_satellite[i, 1] = yNorth
            NED_coords_satellite[i, 2] = zUp
        self.neigh = NearestNeighbors(n_neighbors=1)
        self.neigh.fit(NED_coords_satellite)

        self.file_name = []
        test_df = pd.read_csv(os.path.join(self.root, test_csv_file_name))
        ignore_df = pd.read_csv(os.path.join(self.root, ignore_csv_file_name))

        # get original image & location information
        dirs = os.listdir(os.path.join(self.root, grdimage_dir))
        for director in dirs:
            # director: such as 2011_09_26
            if '2011' not in director:
                continue

            # get drive dir
            subdirs = os.listdir(os.path.join(self.root, grdimage_dir, director))
            for subdir in subdirs:
                # subdir: such as 2011_09_26_drive_0019_sync
                if 'drive' not in subdir:
                    continue

                if subdir in ignore_df.values:
                    continue

                # check train & val from split
                if split=='train':
                    # train, ignore subdir in test.csv
                    if subdir in test_df.values:
                        continue
                else:
                    # test, ignore subdir not in test.csv
                    if subdir not in test_df.values:
                        continue

                items = os.listdir(os.path.join(self.root, grdimage_dir, director, subdir, left_color_camera_dir))

                tmp_file_name = []
                # order items
                items.sort()
                for item in items:
                    if 'png' not in item.lower():
                        continue

                    # file name
                    f_name = os.path.join(director, subdir, item)
                    self.file_name.append(f_name)

    def __len__(self):
        return len(self.file_name)

    def __getitem__(self, idx):
        # read cemera k matrix from camera calibration files, day_dir is first 10 chat of file name
        file_name = self.file_name[idx]

        # debug
        #file_name = '2011_09_26/2011_09_26_drive_0002_sync/0000000034.png'
        #file_name = '2011_09_30/2011_09_30_drive_0016_sync/0000000075.png'


        day_dir = file_name[:10]
        drive_dir = file_name[:38]
        image_no = file_name[38:]

        # get calibration information, do not adjust image size change here
        camera_k, camera_R, camera_t, camera_P = read_calib(
            os.path.join(self.root, grdimage_dir, day_dir, 'calib_cam_to_cam.txt'))
        imu2lidar_R, imu2lidar_T, imu2lidar_H = read_sensor_rel_pose(
            os.path.join(self.root, grdimage_dir, day_dir, 'calib_imu_to_velo.txt'))
        lidar2cam_R, lidar2cam_T, lidar2cam_H = read_sensor_rel_pose(
            os.path.join(self.root, grdimage_dir, day_dir, 'calib_velo_to_cam.txt'))
        # computer the relative pose between imu to camera
        imu2cam_H = lidar2cam_H @ imu2lidar_H
        imu2cam_H[:3, 3] += camera_t
        imu2cam_eulers = euler_from_matrix(imu2cam_H[:3, :3])
        camera_center_loc = -imu2cam_H[:3, :3].T @ imu2cam_H[:3, 3]

        # get location & rotation
        oxts_file_name = os.path.join(self.root, grdimage_dir, drive_dir, oxts_dir,
                                      image_no.lower().replace('.png', '.txt'))
        with open(oxts_file_name, 'r') as f:
            content = f.readline().split(' ')
        location = [float(content[0]), float(content[1]), float(content[2])]
        roll, pitch, heading = float(content[3]), float(content[4]), float(content[5])
        imu_rot = euler_matrix(roll, pitch, heading)

        # read lidar points
        velodyne_file_name = os.path.join(self.root, grdimage_dir, drive_dir, vel_dir,
                                          image_no.lower().replace('.png', '.bin'))
        velodyne = np.fromfile(velodyne_file_name, dtype=np.float32).reshape(-1, 4)
        velodyne[:, 3] = 1.0

        # load depth_map
        # depth_map_name = os.path.join(depth_map_dir, drive_dir,image_no.lower())
        #
        # with Image.open(depth_map_name, 'r') as depthMap:
        #     depth_img = depthMap.convert('I')
        #     depth_map = self.depth_transform(np.array(depth_img))
        #     depth_map = depth_map / 256
        #     depth_map = 0.54 * 718.3351 / depth_map  # in meter #(left_camera_k[0,0]+left_camera_k[1,1])/2
        #     depth_map = torch.where(depth_map > 80., torch.tensor(80.),depth_map)  # ignore more than 80 meter
        #     # turn meter to ratio on sat map
        #     meter_per_pixel = utils.get_meter_per_pixel()
        #     depth_map /= meter_per_pixel  # pixel
        #     satmap_size = utils.get_process_satmap_edge()
        #     depth_map /= satmap_size / 2  # normalize to [0~1] # need to ignore depth more than 1???
        #     grd_left = torch.cat([grd_left, depth_map.float()], dim=0)

        # # key ponints
        # name = drive_dir[:10] + '_' + drive_dir[28:32] + '_' + image_no
        # keypoints = self.keypoints[name]
        # keypoints = torch.tensor(keypoints).float()

        # ground images, left color camera
        left_img_name = os.path.join(self.root, grdimage_dir, drive_dir, left_color_camera_dir, image_no.lower())
        with Image.open(left_img_name, 'r') as GrdImg:
            grd_left = GrdImg.convert('RGB')
            grd_ori_H = grd_left.size[1]
            grd_ori_W = grd_left.size[0]
            # pad right and bottom
            grd_left = transforms.functional.pad(grd_left, (0, 0, grd_pad_size[1]-grd_ori_W,grd_pad_size[0]-grd_ori_H))
            grd_left = ToTensor(grd_left)

        # project these lidar points to image
        # vis_2d: lidar points projected 2D position in the image
        # velodyne_local: lidar points (3D) which are visible to the image, in the velodyne coordinate system
        _, cam_3d, velodyne_local = project_lidar_to_cam(velodyne, camera_P, camera_k, camera_R, camera_t,
                                                         lidar2cam_R, lidar2cam_T, (grd_ori_H,grd_ori_W))
        # transform these velodyne_local to the IMU coordinate system
        velodyne_imu = np.linalg.inv(imu2lidar_H) @ velodyne_local

        # satellite map
        x, y, z = gps_func.GeodeticToEcef(location[0] * np.pi / 180.0, location[1] * np.pi / 180.0, location[2])
        xEast, yNorth, zUp = gps_func.EcefToEnu(x, y, z, self.Geodetic[0, 0] * np.pi / 180.0,
                                                self.Geodetic[0, 1] * np.pi / 180.0, self.Geodetic[0, 2])
        NED_coords_query = np.array([[xEast, yNorth, zUp]])
        distances, indices = self.neigh.kneighbors(NED_coords_query, return_distance=True)
        distances = distances.ravel()[0]
        indices = indices.ravel()[0]
        # find sat image
        sat_gps = self.Geodetic[indices]
        SatMap_name = os.path.join(root_dir, satmap_dir) + "/i" + str(indices) + "_lat_" + str(
            sat_gps[0]) + "_long_" + str(
            sat_gps[1]) + "_zoom_" + str(
            20) + "_size_" + str(640) + "x" + str(640) + "_scale_" + str(2) + ".png"
        with Image.open(SatMap_name, 'r') as SatMap:
            sat_map = SatMap.convert('RGB')
            sat_map = ToTensor(sat_map)

        # get ground-view, satellite image shift
        x_sg, y_sg = gps_func.angular_distance_to_xy_distance_v2(sat_gps[0], sat_gps[1], location[0],
                                                                 location[1])
        meter_per_pixel = Kitti_utils.get_meter_per_pixel(scale=1)
        x_sg = int(x_sg / meter_per_pixel)
        y_sg = int(-y_sg / meter_per_pixel)
        body_location_x = x_sg + satellite_ori_size / 2.0
        body_location_y = y_sg + satellite_ori_size / 2.0

        # add the offset between camera and body to shift the center to query camera
        dx_cam = -camera_center_loc[1] / meter_per_pixel
        dy_cam = camera_center_loc[0] / meter_per_pixel
        # convert the offset to ploar coordinate
        tan_theta = -camera_center_loc[1] / camera_center_loc[0]
        length_dxy = np.sqrt(
            camera_center_loc[0] * camera_center_loc[0] + camera_center_loc[1] * camera_center_loc[1])
        offset_cam_theta = np.arctan(tan_theta)
        yaw_FL = heading - offset_cam_theta
        dx_cam_pixel = np.cos(yaw_FL) * length_dxy / meter_per_pixel
        dy_cam_pixel = -np.sin(yaw_FL) * length_dxy / meter_per_pixel
        cam_location_x = x_sg + satellite_ori_size / 2.0 + dx_cam_pixel
        cam_location_y = y_sg + satellite_ori_size / 2.0 + dy_cam_pixel

        # # get velodyne_world on the satelite map
        velodyne_sat_2d, velodyne_sat, velodyne_imu, sat_mask, imu2sat = project_lidar_to_sat(velodyne_imu, imu_rot, meter_per_pixel, [body_location_x, body_location_y])
        velodyne_grd = cam_3d[:,sat_mask].T

        # check max_num_points
        num_diff = self.conf.max_num_points3D - len(velodyne_sat)
        if num_diff < 0:
            valid_idx = np.random.choice(range(len(velodyne_sat)), self.conf.max_num_points3D)
            velodyne_sat = velodyne_sat[valid_idx]
            velodyne_grd = velodyne_grd[valid_idx]
        elif num_diff > 0 and self.conf.force_num_points3D:
            sat_add = np.ones((num_diff, 3)) * velodyne_sat[-1]
            grd_add = np.ones((num_diff, 3)) * velodyne_grd[-1]
            velodyne_sat = np.vstack((velodyne_sat, sat_add))
            velodyne_grd = np.vstack((velodyne_grd, grd_add))

        # # with yaw embedding
        # _, H, W = grd_left.size()
        # fov, min_fov = utils.get_grd_fov()
        # yaw_range = (torch.arange(W) * fov / W + min_fov) / 180  # -1~1
        # yaw_range = yaw_range+heading #[W]
        # yaw_range = torch.where(yaw_range > 1, yaw_range - 2, yaw_range)
        # yaw_range = torch.where(yaw_range < -1, yaw_range + 2, yaw_range)
        # yaw_range = yaw_range.view(1,1,W).repeat(1, H, 1).float()
        # grd_left = torch.cat([grd_left, yaw_range], dim=0)
        # yaw and dis embedding
        # sat_map = torch.cat([sat_map,self.sat_dis.clone(),self.sat_yaw.clone()],dim=0)


        # sat 
        camera = Camera.from_colmap(dict(
            model='SIMPLE_PINHOLE', params=(1 / meter_per_pixel, body_location_x, body_location_y, 0,0,0,0,np.infty),#np.infty for parallel projection
            width=int(satellite_ori_size), height=int(satellite_ori_size)))
        sat_image = {
            'image': sat_map.float(),
            'camera': camera.float(),
            'T_w2cam': Pose.from_4x4mat(imu2sat).float(),
            'points3D': torch.from_numpy(velodyne_sat).float()  # world is imu, from imu to sat
        }

        # grd
        camera_para = (camera_k[0,0],camera_k[1,1],camera_k[0,2],camera_k[1,2])
        camera = Camera.from_colmap(dict(
            model='PINHOLE', params=camera_para,
            width=int(grd_pad_size[1]), height=int(grd_pad_size[0])))
        grd_image = {
            'image': grd_left.float(),
            'camera': camera.float(),
            'T_w2cam': Pose.from_4x4mat(imu2cam_H).float(),
            'points3D': torch.from_numpy(velodyne_grd).float()  # world is imu
        }

        r2q_gt = grd_image['T_w2cam'] @ sat_image['T_w2cam'].inv() # query:grd
        #r2q_gt = sat_image['T_w2cam'] @ grd_image['T_w2cam'].inv() # query:sat
        # ramdom shift translation and ratation on yaw
        YawShiftRange = 2 * np.pi / 180 # in 10 degree
        yaw = 2 * YawShiftRange * np.random.random() - YawShiftRange
        R_yaw = torch.tensor([[np.cos(yaw),-np.sin(yaw),0],[np.sin(yaw),np.cos(yaw),0],[0,0,1]])

        TShiftRange = 2# in 2 meter
        T = 2 * TShiftRange * np.random.rand((3)) - TShiftRange
        T[2] = 0 # no shift on height

        r2q_init = Pose.from_Rt(R_yaw,T).float()
        r2q_init = r2q_gt@r2q_init

        # scene
        scene = drive_dir[:4]+drive_dir[5:7]+drive_dir[8:10]+drive_dir[28:32]+image_no[:10]

        data = {
            'ref': grd_image, #sat_image,#
            'query': sat_image, #grd_image,#
            'overlap': 0.5,
            'T_r2q_init': r2q_init,
            'T_r2q_gt': r2q_gt, 
            'scene': scene
        }

        # debug
        if 0:
            fig = plt.figure(figsize=plt.figaspect(0.5))
            ax1 = fig.add_subplot(1, 2, 1)
            ax2 = fig.add_subplot(1, 2, 2)

            #img_0 = cv2.imread(left_img_name, cv2.IMREAD_GRAYSCALE)
            #img_1 = cv2.imread(SatMap_name, cv2.IMREAD_GRAYSCALE)
            # color_image0 = cv2.cvtColor(img_0, cv2.COLOR_GRAY2RGB)
            # color_image1 = cv2.cvtColor(img_1, cv2.COLOR_GRAY2RGB)
            color_image0 = transforms.functional.to_pil_image(grd_left, mode='RGB')
            color_image0 = np.array(color_image0)
            color_image1 = transforms.functional.to_pil_image(sat_map, mode='RGB')
            color_image1 = np.array(color_image1)
            # get velodyne_world on the satelite map
            # velodyne_grd = grd_image['T_w2cam'] * velodyne_imu # imu2camera
            grd_2d, _ = grd_image['camera'].world2image(velodyne_grd) ##camera 3d to 2d
            grd_2d = grd_2d.T
            for j in range(grd_2d.shape[1]):
                cv2.circle(color_image0, (np.int32(grd_2d[0][j]), np.int32(grd_2d[1][j])), 2, (255, 0, 0),
                           -1)
            # velodyne_sat = sat_image['T_w2cam'] * velodyne_imu
            sat_2d, _ = sat_image['camera'].world2image(velodyne_sat)  ##camera 3d to 2d
            sat_2d = sat_2d.T
            for j in range(sat_2d.shape[1]):
                cv2.circle(color_image1, (np.int32(sat_2d[0][j]), np.int32(sat_2d[1][j])), 2, (255, 0, 0),
                           -1)

            # sat to grd gt green
            cam_3d = r2q * velodyne_sat
            grd_2d, _ = grd_image['camera'].world2image(cam_3d)  ##camera 3d to 2d
            grd_2d = grd_2d.T
            for j in range(grd_2d.shape[1]):
                cv2.circle(color_image0, (np.int32(grd_2d[0][j]), np.int32(grd_2d[1][j])), 2, (0, 255, 0),
                           -1)
            # sat to grd init blue
            cam_3d = r2q_init * velodyne_sat
            grd_2d, _ = grd_image['camera'].world2image(cam_3d)  ##camera 3d to 2d
            grd_2d = grd_2d.T
            for j in range(grd_2d.shape[1]):
                cv2.circle(color_image0, (np.int32(grd_2d[0][j]), np.int32(grd_2d[1][j])), 2, (0, 0, 255),
                           -1)

            ax1.imshow(color_image0)
            ax2.imshow(color_image1)
            # camera position
            ax2.scatter(x=cam_location_x, y=cam_location_y, c='r', s=30)
            ax2.scatter(x=body_location_x, y=body_location_y, c='g', s=20)
            # plot the direction of the body frame
            length_xy = 200.0
            origin = np.array([[body_location_x], [body_location_y]])  # origin point
            dx_east = length_xy * np.cos(heading)
            dy_north = length_xy * np.sin(heading)
            V = np.array([[dx_east, dy_north]])
            ax2.quiver(*origin, V[:, 0], V[:, 1], color=['r'], scale=1000)
            plt.show()

        return data

# def load_val_data(batch_size):
#     # SatMap_process_edge = utils.get_process_satmap_edge()
#
#     if visualise_debug:
#         satmap_transform = transforms.Compose([
#             # transforms.Resize(size=[SatMap_process_edge,SatMap_process_edge]),
#             transforms.ToTensor(),
#         ])
#     else:
#         satmap_transform = transforms.Compose([
#             # transforms.Resize(size=[SatMap_process_edge,SatMap_process_edge]),
#             transforms.ToTensor(),
#             transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
#         ])
#
#     # Grd_h = GrdImg_H
#     # Grd_w = GrdImg_W
#
#     if visualise_debug:
#         grdimage_transform = transforms.Compose([
#             # transforms.Resize(size=[Grd_h,Grd_w]),
#             transforms.ToTensor(),
#         ])
#     else:
#         grdimage_transform = transforms.Compose([
#             # transforms.Resize(size=[Grd_h,Grd_w]),
#             transforms.ToTensor(),
#             transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
#         ])  # 0~1 to -1~1
#     depth_transform = transforms.Compose([
#         transforms.ToTensor(),
#         # transforms.Resize(size=[Grd_h, Grd_w]),
#     ])
#
#     test_dataset = SatGrdDataset(root=root_dir, train_mode=False,
#                                  transform=(satmap_transform, grdimage_transform, depth_transform))
#     test_loader = DataLoader(test_dataset, batch_size=batch_size, shuffle=True, pin_memory=True,
#                              num_workers=num_thread_workers, drop_last=False)
#     return test_loader
#
#
# def load_train_data(batch_size):
#     # SatMap_process_edge = utils.get_process_satmap_edge()
#
#     satmap_transform = transforms.Compose([
#         # transforms.Resize(size=[SatMap_process_edge,SatMap_process_edge]),
#         transforms.ToTensor(),
#         transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])  # test
#     ])
#
#     # Grd_h = GrdImg_H
#     # Grd_w = GrdImg_W
#
#     grdimage_transform = transforms.Compose([
#         # transforms.Resize(size=[Grd_h,Grd_w]),
#         transforms.ColorJitter(brightness=0.4, contrast=0.4, saturation=0.4, hue=0.4),
#         transforms.ToTensor(),
#         transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
#     ])
#
#     depth_transform = transforms.Compose([
#         transforms.ToTensor(),
#         # transforms.Resize(size=[Grd_h, Grd_w]),
#     ])
#
#     train_set = SatGrdDataset(root=root_dir, train_mode=True,
#                               transform=(satmap_transform, grdimage_transform, depth_transform))
#     train_loader = DataLoader(train_set, batch_size=batch_size, shuffle=True, pin_memory=True,
#                               num_workers=num_thread_workers, drop_last=False)
#     return train_loader
#
#
if __name__ == '__main__':
    # test to load 1 data
    conf = {
        'min_overlap': 0.3,  # ?
        'max_overlap': 1.0,  # ?
        'max_num_points3D': 20000,
        'force_num_points3D': True,
        'batch_size': 1,
        'min_baseline': 1.,
        'max_baseline': 7.,

        'resize': 720,  # ?
        'resize_by': 'min',
        'crop': 720,  # ?
        'optimal_crop': False,
        'seed': 1,
        'num_workers': 0,
    }
    dataset = Kitti(conf)
    loader = dataset.get_data_loader('train', shuffle=True)  # or 'train' ‘val’

    for _, data in zip(range(1), loader):
        print(data)


