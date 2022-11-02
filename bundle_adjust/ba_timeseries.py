"""
A Generic Bundle Adjustment Methodology for Indirect RPC Model Refinement of Satellite Imagery
code for Image Processing On Line https://www.ipol.im/

author: Roger Mari <roger.mari@ens-paris-saclay.fr>

This script implements the Scene class
This class loads a timeseries of satellite images and the associated RPC models
If there is only one acquisition date, then the timeseries has length 1
The data is prepared to feed a bundle adjustment pipeline and correct the camera models
"""


import numpy as np
import os
import sys
import timeit
import glob
import rpcm
import json
import shutil

from bundle_adjust import loader, ba_utils, geo_utils, cam_utils
from bundle_adjust.ba_pipeline import BundleAdjustmentPipeline
from bundle_adjust.loader import flush_print
import re

def get_acquisition_date(geotiff_path):
    """
    Reads the acquisition date of a geotiff
    """
    import datetime
    import rasterio

    with rasterio.open(geotiff_path) as src:
        print("src tags: ", src.tags(), src.tags().keys())
        if "TIFFTAG_DATETIME" in src.tags().keys():
            date_string = src.tags()["TIFFTAG_DATETIME"]
            dt = datetime.datetime.strptime(date_string, "%Y:%m:%d %H:%M:%S")
        elif "METADATATYPE" in src.tags().keys(): # pleiades
            print("hello: ", geotiff_path)
            matched = re.match(r"^.*IMG_PHR(.*)_([0-9]*)_SEN_.*$", str(geotiff_path))
            date_string = matched.group(2)
            print("date_string: ", date_string) 

            dt = datetime.datetime.strptime(date_string, "%Y%m%d%H%M%S%f")
        elif "AREA_OR_POINT" in src.tags().keys(): # pneo
            print("world: ", geotiff_path)
            matched = re.match(r"^.*IMG_P(.*)_([0-9]*)_PAN_SEN_.*$", str(geotiff_path))
            date_string = matched.group(2)
            print("date_string: ", date_string) 

            dt = datetime.datetime.strptime(date_string, "%Y%m%d%H%M%S%f")
        else:
            # temporary fix in case the previous tag is missing
            # get datetime from skysat geotiff identifier
            date_string = os.path.basename(geotiff_path)[:15]
            dt = datetime.datetime.strptime(date_string, "%Y%m%d_%H%M%S")
    return dt


def group_files_by_date(datetimes, image_fnames):
    """
    This function picks a list of image fnames and their acquisition dates,
    and returns the timeline of a scene to bundle adjust (class Scene fromfrom ba_timeseries.py)
    Each timeline instance is a group of images with a common acquisition date (i.e. less than 30 mins difference)
    """

    def dt_diff_in_mins(d1, d2):
        return abs((d1 - d2).total_seconds() / 60.0)

    # sort images according to the acquisition date
    sorted_indices = np.argsort(datetimes)
    sorted_datetimes = np.array(datetimes)[sorted_indices].tolist()
    sorted_fnames = np.array(image_fnames)[sorted_indices].tolist()
    margin = 30  # maximum acquisition time difference allowed, in minutes, within a timeline instance

    # build timeline
    d = {}
    dates_already_seen = []
    for im_idx, fname in enumerate(sorted_fnames):

        new_date = True
        current_dt = sorted_datetimes[im_idx]
        diff_wrt_prev_dates_in_mins = [dt_diff_in_mins(x, current_dt) for x in dates_already_seen]

        if len(diff_wrt_prev_dates_in_mins) > 0:
            min_pos = np.argmin(diff_wrt_prev_dates_in_mins)

            # if this image was acquired within 30 mins of difference w.r.t to an already seen date,
            # then it is part of the same acquisition
            if diff_wrt_prev_dates_in_mins[min_pos] < margin:
                ref_date_id = dates_already_seen[min_pos].strftime("%Y%m%d_%H%M%S")
                d[ref_date_id].append(im_idx)
                new_date = False

        if new_date:
            date_id = sorted_datetimes[im_idx].strftime("%Y%m%d_%H%M%S")
            d[date_id] = [im_idx]
            dates_already_seen.append(sorted_datetimes[im_idx])

    timeline = []
    for k in d.keys():
        current_datetime = sorted_datetimes[d[k][0]]
        im_fnames_current_datetime = np.array(sorted_fnames)[d[k]].tolist()
        timeline.append(
            {
                "datetime": current_datetime,
                "id": k.split("/")[-1],
                "fnames": im_fnames_current_datetime,
                "n_images": len(d[k]),
                "adjusted": False,
                "image_weights": [],
            }
        )
    return timeline


class Error(Exception):
    pass


class Scene:
    def __init__(self, scene_config):

        t0 = timeit.default_timer()
        args = loader.load_dict_from_json(scene_config)

        # read scene args
        self.geotiff_dir = args["geotiff_dir"]
        self.rpc_dir = args["rpc_dir"]
        self.rpc_src = args["rpc_src"]
        self.dst_dir = args["output_dir"]

        # optional arguments to handle timeseries
        self.ba_method = args.get("ba_method", "ba_bruteforce")
        self.selected_timeline_indices = args.get("timeline_indices", None)
        self.geotiff_label = args.get("geotiff_label", None)
        self.n_dates = int(args.get("n_dates", 1))

        # optional arguments for bundle adjustment configuration
        self.cam_model = args.get("cam_model", "rpc")
        self.correction_params = args.get("correction_params", ["R"])
        self.predefined_matches = args.get("predefined_matches", False)
        self.fix_ref_cam = args.get("fix_ref_cam", False)
        self.ref_cam_weight = float(args.get("ref_cam_weight", 1))
        self.clean_outliers = args.get("clean_outliers", True)
        self.reset = args.get("reset", True)
        self.remove_FT_files = args.get("remove_FT_files", False)

        # check geotiff_dir and rpc_dir exists
        if not os.path.isdir(self.geotiff_dir):
            raise Error('geotiff_dir "{}" does not exist'.format(self.geotiff_dir))
        if not os.path.isdir(self.rpc_dir):
            raise Error('rpc_dir "{}" does not exist'.format(self.rpc_dir))
        for v in self.correction_params:
            if v not in ["R", "T", "K", "COMMON_K"]:
                raise Error("{} is not a valid camera parameter to optimize".format(v))

        # create output path
        os.makedirs(self.dst_dir, exist_ok=True)

        # needed to run bundle adjustment
        self.init_ba_input_data()

        # feature tracks configuration
        from .feature_tracks.ft_utils import init_feature_tracks_config

        self.tracks_config = init_feature_tracks_config()
        for k in self.tracks_config.keys():
            if k in args.keys():
                self.tracks_config[k] = args[k]

        print("\n###################################################################################")
        print("\nLoading scene from {}\n".format(scene_config))
        print("-------------------------------------------------------------")
        print("Configuration:")
        print("    - geotiff_dir:   {}".format(self.geotiff_dir))
        print("    - rpc_dir:       {}".format(self.rpc_dir))
        print("    - rpc_src:       {}".format(self.rpc_src))
        print("    - output_dir:    {}".format(self.dst_dir))
        print("    - cam_model:     {}".format(self.cam_model))
        flush_print("-------------------------------------------------------------\n")

        # construct scene timeline
        self.aoi_lonlat = None
        self.timeline = self.load_scene()
        # if aoi_geojson is not defined in ba_config define aoi_lonlat as the union of all geotiff footprints
        if "aoi_geojson" in args.keys():
            self.aoi_lonlat = loader.load_geojson(args["aoi_geojson"])
            print("AOI geojson loaded from {}".format(args["aoi_geojson"]))
            loader.save_geojson("{}/AOI_init.json".format(self.dst_dir), self.aoi_lonlat)

        start_date = self.timeline[0]["datetime"].date()
        end_date = self.timeline[-1]["datetime"].date()
        print("Number of acquisition dates: {} (from {} to {})".format(len(self.timeline), start_date, end_date))
        print("Number of images: {}".format(np.sum([d["n_images"] for d in self.timeline])))
        print("Scene loaded in {:.2f} seconds".format(timeit.default_timer() - t0))
        flush_print("\n###################################################################################\n\n")

    def load_scene(self):

        all_im_fnames = []
        all_im_rpcs = []
        all_im_datetimes = []

        geotiff_paths = sorted(glob.glob(os.path.join(self.geotiff_dir, "**/*.JP2"), recursive=True))
        if self.geotiff_label is not None:
            geotiff_paths = [os.path.basename(fn) for fn in geotiff_paths if self.geotiff_label in fn]
        print("geotiff_paths: ", geotiff_paths)
        for tif_fname in geotiff_paths:

            f_id = loader.get_id(tif_fname)

            # load rpc
            if self.rpc_src == "geotiff":
                rpc = rpcm.rpc_from_geotiff(tif_fname)
                print("rpc: ", rpc)
            elif self.rpc_src == "json":
                with open(os.path.join(self.rpc_dir, f_id + ".json")) as f:
                    d = json.load(f)
                rpc = rpcm.RPCModel(d, dict_format="rpcm")
            elif self.rpc_src == "txt":
                rpc = rpcm.rpc_from_rpc_file(os.path.join(self.rpc_dir, f_id + ".rpc"))
            else:
                raise ValueError("Unknown rpc_src value: {}".format(self.rpc_src))

            all_im_fnames.append(tif_fname)
            all_im_rpcs.append(rpc)
            all_im_datetimes.append(get_acquisition_date(tif_fname))

        # copy initial rpcs
        init_rpcs_dir = os.path.join(self.dst_dir, "rpcs_init")
        all_rpc_fnames = ["{}/{}.rpc".format(init_rpcs_dir, loader.get_id(fn)) for fn in all_im_fnames]
        loader.save_rpcs(all_rpc_fnames, all_im_rpcs)

        # define timeline and aoi
        timeline = group_files_by_date(all_im_datetimes, all_im_fnames)

        return timeline

    def get_timeline_attributes(self, timeline_indices, attributes):
        """
        Displays the value of certain attributes at some indices of the timeline in a scene to bundle adjust
        """

        max_lens = np.zeros(len(attributes)).tolist()
        for idx in timeline_indices:
            to_display = ""
            for a_idx, a in enumerate(attributes):
                string_len = len("{}".format(self.timeline[idx][a]))
                if max_lens[a_idx] < string_len:
                    max_lens[a_idx] = string_len
        max_len_idx = max([len(str(idx)) for idx in timeline_indices])
        index_str = "index"
        margin = max_len_idx - len(index_str)
        header_values = [index_str + " " * margin] if margin > 0 else [index_str]
        for a_idx, a in enumerate(attributes):
            margin = max_lens[a_idx] - len(a)
            header_values.append(a + " " * margin if margin > 0 else a)
        header_row = "  |  ".join(header_values)
        print(header_row)
        print("_" * len(header_row) + "\n")
        for idx in timeline_indices:
            margin = len(header_values[0]) - len(str(idx))
            to_display = [str(idx) + " " * margin if margin > 0 else str(idx)]
            for a_idx, a in enumerate(attributes):
                a_value = "{}".format(self.timeline[idx][a])
                margin = len(header_values[a_idx + 1]) - len(a_value)
                to_display.append(a_value + " " * margin if margin > 0 else a_value)
            print("  |  ".join(to_display))

        if "n_images" in attributes:  # add total number of images
            print("_" * len(header_row) + "\n")
            to_display = [" " * len(header_values[0])]
            for a_idx, a in enumerate(attributes):
                if a == "n_images":
                    a_value = "{} total".format(sum([self.timeline[idx]["n_images"] for idx in timeline_indices]))
                    margin = len(header_values[a_idx + 1]) - len(a_value)
                    to_display.append(a_value + " " * margin if margin > 0 else a_value)
                else:
                    to_display.append(" " * len(header_values[a_idx + 1]))
            print("     ".join(to_display))
        print("\n")

    def check_adjusted_dates(self, input_dir, t_idx):

        prev_adj_data_found = False
        dir_adj_rpc = os.path.join(input_dir, "rpcs_adj")
        if os.path.isdir(dir_adj_rpc):
            adj_fnames = []
            for adj_id in [loader.get_id(p) for p in glob.glob(dir_adj_rpc + "/*.rpc_adj")]:
                adj_fnames.extend(glob.glob(os.path.join(self.geotiff_dir, "**/" + adj_id + ".tif"), recursive=True))
            print("Found {} previously adjusted images in {}\n".format(len(adj_fnames), self.dst_dir))

            datetimes_adj = [get_acquisition_date(img_geotiff_path) for img_geotiff_path in adj_fnames]
            timeline_adj = group_files_by_date(datetimes_adj, adj_fnames)
            for d in timeline_adj:
                adj_id = d["id"]
                for idx in range(len(self.timeline)):
                    if self.timeline[idx]["id"] == adj_id and idx < t_idx:
                        self.timeline[idx]["adjusted"] = True
                        prev_adj_data_found = True

        if not prev_adj_data_found:
            print("No previously adjusted data was found in {}\n".format(self.dst_dir))

        return prev_adj_data_found

    def load_data_from_dates(self, timeline_indices, input_dir, adjusted=False):

        im_fnames = []
        for t_idx in timeline_indices:
            im_fnames.extend(self.timeline[t_idx]["fnames"])
        n_cam = len(im_fnames)
        to_print = [n_cam, "adjusted" if adjusted else "new"]
        flush_print("{} images for bundle adjustment !".format(*to_print))

        images = []
        if n_cam > 0:
            # get rpcs
            rpc_dir = os.path.join(input_dir, "rpcs_adj") if adjusted else os.path.join(self.dst_dir, "rpcs_init")
            extension = "rpc_adj" if adjusted else "rpc"
            im_rpcs = loader.load_rpcs_from_dir(im_fnames, rpc_dir, extension=extension, verbose=True)
            for fn, rpc in zip(im_fnames, im_rpcs):
                images.append(cam_utils.SatelliteImage(fn, rpc))

        if adjusted:
            self.n_adj += n_cam
            self.images_adj.extend(images)
        else:
            self.n_adj += 0
            self.images_new.extend(images)

    def load_prev_adjusted_dates(self, t_idx, input_dir, previous_dates=1):

        # t_idx = timeline index of the new date to adjust
        dt2str = lambda t: t.strftime("%Y-%m-%d %H:%M:%S")
        found_adj_dates = self.check_adjusted_dates(input_dir, t_idx)
        if found_adj_dates:
            # load data from closest date in time
            all_prev_adj_t_indices = [idx for idx, d in enumerate(self.timeline) if d["adjusted"]]
            closest_adj_t_indices = sorted(all_prev_adj_t_indices, key=lambda x: abs(x - t_idx))
            adj_t_indices_to_use = closest_adj_t_indices[:previous_dates]
            adj_dates_to_use = ", ".join([dt2str(self.timeline[k]["datetime"]) for k in adj_t_indices_to_use])
            print("Using {} previously adjusted date(s): {}\n".format(len(adj_t_indices_to_use), adj_dates_to_use))
            self.load_data_from_dates(adj_t_indices_to_use, input_dir, adjusted=True)

    def init_ba_input_data(self):
        self.n_adj = 0
        self.images_adj = []
        self.images_new = []

    def set_ba_input_data(self, t_indices, input_dir, output_dir, previous_dates):

        print("\n\n\nSetting bundle adjustment input data...\n")
        # init
        self.init_ba_input_data()
        # load previously adjusted data (if existent) relevant for the current date
        if previous_dates > 0:
            self.load_prev_adjusted_dates(min(t_indices), input_dir, previous_dates=previous_dates)
        # load new data to adjust
        self.load_data_from_dates(t_indices, input_dir)

        self.ba_data = {}
        self.ba_data["in_dir"] = input_dir
        self.ba_data["out_dir"] = output_dir
        self.ba_data["images"] = self.images_adj + self.images_new
        flush_print("\n...bundle adjustment input data is ready !\n\n")

    def bundle_adjust(self, feature_detection=True):

        import timeit

        t0 = timeit.default_timer()

        extra_ba_config = {}
        extra_ba_config["cam_model"] = self.cam_model
        if self.aoi_lonlat is not None:
            extra_ba_config["aoi"] = self.aoi_lonlat
        extra_ba_config["n_adj"] = self.n_adj
        extra_ba_config["correction_params"] = self.correction_params
        extra_ba_config["predefined_matches"] = self.predefined_matches
        extra_ba_config["fix_ref_cam"] = self.fix_ref_cam
        extra_ba_config["ref_cam_weight"] = self.ref_cam_weight
        extra_ba_config["clean_outliers"] = self.clean_outliers

        # run bundle adjustment
        self.ba_pipeline = BundleAdjustmentPipeline(self.ba_data, self.tracks_config, extra_ba_config)
        self.ba_pipeline.run()

        # retrieve some stuff for verbose
        n_tracks = self.ba_pipeline.ba_params.pts3d_ba.shape[0]
        elapsed_time = timeit.default_timer() - t0
        ba_e, init_e = np.mean(self.ba_pipeline.ba_e), np.mean(self.ba_pipeline.init_e)
        elapsed_time_FT = self.ba_pipeline.feature_tracks_running_time

        return elapsed_time, elapsed_time_FT, n_tracks, ba_e, init_e

    def rm_tmp_files_after_ba(self):
        shutil.rmtree("{}/{}/matches".format(self.dst_dir, self.ba_method))

    def reset_ba_params(self):
        ba_dir = "{}/{}".format(self.dst_dir, self.ba_method)
        if os.path.exists(ba_dir):
            shutil.rmtree(ba_dir)
        for t_idx in range(len(self.timeline)):
            self.timeline[t_idx]["adjusted"] = False

    def run_sequential_bundle_adjustment(self):

        ba_dir = os.path.join(self.dst_dir, self.ba_method)
        os.makedirs(ba_dir, exist_ok=True)

        n_input_dates = len(self.selected_timeline_indices)
        self.tracks_config["FT_predefined_pairs"] = []

        time_per_date, time_per_date_FT, ba_iters_per_date = [], [], []
        tracks_per_date, init_e_per_date, ba_e_per_date = [], [], []
        for idx, t_idx in enumerate(self.selected_timeline_indices):
            self.set_ba_input_data([t_idx], ba_dir, ba_dir, self.n_dates)
            if (idx == 0 and self.fix_ref_cam) or (self.n_dates == 0 and self.fix_ref_cam):
                self.fix_ref_cam = True
            else:
                self.fix_ref_cam = False
            running_time, time_FT, n_tracks, ba_e, _ = self.bundle_adjust()
            pts_out_fn = "{}/pts3d_adj/{}_pts3d_adj.ply".format(ba_dir, self.timeline[t_idx]["id"])
            os.makedirs(os.path.dirname(pts_out_fn), exist_ok=True)
            shutil.copyfile(ba_dir + "/pts3d_adj.ply", pts_out_fn)

            # initial error by bundle_adjust() is not representative here in sequential mode
            # this is because rpcs from previous dates are not the original ones
            init_e, _ = self.compute_reprojection_error_before_and_after_bundle_adjust()
            time_per_date.append(running_time)
            time_per_date_FT.append(time_FT)
            tracks_per_date.append(n_tracks)
            init_e_per_date.append(init_e)
            ba_e_per_date.append(ba_e)
            ba_iters_per_date.append(self.ba_pipeline.ba_iters)
            current_dt = self.timeline[t_idx]["datetime"]
            to_print = [idx + 1, n_input_dates, current_dt, running_time, n_tracks, init_e, ba_e]
            flush_print("({}/{}) {} adjusted in {:.2f} seconds, {} ({:.3f}, {:.3f})".format(*to_print))

        if self.remove_FT_files:
            self.rm_tmp_files_after_ba()
        total_time = sum(time_per_date)
        avg_tracks_per_date = int(np.ceil(np.mean(tracks_per_date)))
        to_print = [total_time, avg_tracks_per_date, np.mean(init_e_per_date), np.mean(ba_e_per_date)]
        flush_print("All dates adjusted in {:.2f} seconds, {} ({:.3f}, {:.3f})".format(*to_print))
        time_FT = loader.get_time_in_hours_mins_secs(sum(time_per_date_FT))
        flush_print("\nAll feature tracks constructed in {}\n".format(time_FT))
        flush_print("Average BA iterations per date: {}".format(int(np.ceil(np.mean(ba_iters_per_date)))))
        flush_print("\nTOTAL TIME: {}\n".format(loader.get_time_in_hours_mins_secs(total_time)))

    def run_global_bundle_adjustment(self):

        ba_dir = os.path.join(self.dst_dir, self.ba_method)
        os.makedirs(ba_dir, exist_ok=True)

        # only pairs from the same date or consecutive dates are allowed
        args = [self.timeline, self.selected_timeline_indices, self.n_dates]
        self.tracks_config["FT_predefined_pairs"] = ba_utils.load_pairs_from_same_date_and_next_dates(*args)

        # load bundle adjustment data and run bundle adjustment
        self.set_ba_input_data(self.selected_timeline_indices, ba_dir, ba_dir, 0)
        running_time, time_FT, n_tracks, ba_e, init_e = self.bundle_adjust()
        if self.remove_FT_files:
            self.rm_tmp_files_after_ba()

        args = [running_time, n_tracks, init_e, ba_e]
        flush_print("All dates adjusted in {:.2f} seconds, {} ({:.3f}, {:.3f})".format(*args))
        time_FT = loader.get_time_in_hours_mins_secs(time_FT)
        flush_print("\nAll feature tracks constructed in {}\n".format(time_FT))
        flush_print("Total BA iterations: {}".format(int(self.ba_pipeline.ba_iters)))
        flush_print("\nTOTAL TIME: {}\n".format(loader.get_time_in_hours_mins_secs(running_time)))

    def run_bruteforce_bundle_adjustment(self):

        ba_dir = os.path.join(self.dst_dir, self.ba_method)
        os.makedirs(ba_dir, exist_ok=True)

        self.tracks_config["FT_predefined_pairs"] = []
        self.set_ba_input_data(self.selected_timeline_indices, ba_dir, ba_dir, 0)
        running_time, time_FT, n_tracks, ba_e, init_e = self.bundle_adjust()
        if self.remove_FT_files:
            self.rm_tmp_files_after_ba()

        args = [running_time, n_tracks, init_e, ba_e]
        flush_print("All dates adjusted in {:.2f} seconds, {} ({:.3f}, {:.3f})".format(*args))
        time_FT = loader.get_time_in_hours_mins_secs(time_FT)
        flush_print("\nAll feature tracks constructed in {}\n".format(time_FT))
        flush_print("Total BA iterations: {}".format(int(self.ba_pipeline.ba_iters)))
        flush_print("\nTOTAL TIME: {}\n".format(loader.get_time_in_hours_mins_secs(running_time)))

    def is_ba_method_valid(self, ba_method):
        return ba_method in ["ba_global", "ba_sequential", "ba_bruteforce"]

    def compute_reprojection_error_before_and_after_bundle_adjust(self):

        im_fnames = [im.geotiff_path for im in self.ba_pipeline.images]
        C = self.ba_pipeline.ba_params.C
        pairs_to_triangulate = self.ba_pipeline.ba_params.pairs_to_triangulate
        cam_model = "rpc"

        # get init and bundle adjusted rpcs
        rpcs_init_dir = os.path.join(self.dst_dir, "rpcs_init")
        rpcs_init = loader.load_rpcs_from_dir(im_fnames, rpcs_init_dir, extension="rpc", verbose=False)
        rpcs_ba_dir = os.path.join(self.dst_dir, self.ba_method + "/rpcs_adj")
        rpcs_ba = loader.load_rpcs_from_dir(im_fnames, rpcs_ba_dir, extension="rpc_adj", verbose=False)

        # triangulate
        from .feature_tracks.ft_triangulate import init_pts3d

        pts3d_before = init_pts3d(C, rpcs_init, cam_model, pairs_to_triangulate, verbose=False)
        pts3d_after = init_pts3d(C, rpcs_ba, cam_model, pairs_to_triangulate, verbose=False)

        # reproject
        n_pts, n_cam = C.shape[1], C.shape[0] // 2
        not_nan_C = ~np.isnan(C)
        err_before, err_after = [], []
        for cam_idx in range(n_cam):
            pt_indices = np.where(not_nan_C[2 * cam_idx])[0]
            obs2d = C[(cam_idx * 2) : (cam_idx * 2 + 2), pt_indices].T
            pts3d_init = pts3d_before[pt_indices, :]
            pts3d_ba = pts3d_after[pt_indices, :]
            args = [rpcs_init[cam_idx], rpcs_ba[cam_idx], cam_model, obs2d, pts3d_init, pts3d_ba]
            _, _, err_b, err_a, _ = ba_utils.reproject_pts3d(*args)
            err_before.extend(err_b.tolist())
            err_after.extend(err_a.tolist())
        return np.mean(err_before), np.mean(err_after)

    def run_bundle_adjustment_for_RPC_refinement(self):

        # read the indices of the selected dates and print some information
        if self.selected_timeline_indices is None:
            self.selected_timeline_indices = np.arange(len(self.timeline), dtype=np.int32).tolist()
            flush_print("All dates selected to bundle adjust!\n")
        else:
            to_print = [len(self.selected_timeline_indices), self.selected_timeline_indices]
            flush_print("Found {} selected dates to bundle adjust! timeline_indices: {}\n".format(*to_print))
            self.get_timeline_attributes(self.selected_timeline_indices, ["datetime", "n_images", "id"])
        for idx, t_idx in enumerate(self.selected_timeline_indices):
            args = [idx + 1, self.timeline[t_idx]["datetime"], self.timeline[t_idx]["n_images"]]
            flush_print("({}) {} --> {} views".format(*args))

        if self.reset:
            self.reset_ba_params()

        # run bundle adjustment
        if self.ba_method == "ba_sequential":
            print("\nRunning sequential bundle adjustment !")
            flush_print("Each date aligned with {} previous date(s)\n".format(self.n_dates))
            self.run_sequential_bundle_adjustment()
        elif self.ba_method == "ba_global":
            print("\nRunning global bundle ajustment !")
            print("All dates will be adjusted together at once")
            flush_print("Track pairs restricted to the same date and the next {} dates\n".format(self.n_dates))
            self.run_global_bundle_adjustment()
        elif self.ba_method == "ba_bruteforce":
            print("\nRunning bruteforce bundle ajustment !")
            flush_print("All dates will be adjusted together at once\n")
            self.run_bruteforce_bundle_adjustment()
        else:
            print("ba_method {} is not valid !".format(self.ba_method))
            print("accepted values are: [ba_sequential, ba_global, ba_bruteforce]")
            sys.exit()
