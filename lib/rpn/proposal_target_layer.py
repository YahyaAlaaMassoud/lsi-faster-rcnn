# --------------------------------------------------------
# LSI-Faster R-CNN
# Original work Copyright (c) 2015 Microsoft
# Modified work Copyright 2018 Carlos Guindel
# Licensed under The MIT License [see LICENSE for details]
# Originally written by Ross Girshick and Sean Bell
# --------------------------------------------------------

import caffe
import yaml
import numpy as np
import numpy.random as npr
from fast_rcnn.config import cfg
from fast_rcnn.bbox_transform import bbox_transform
from utils.cython_bbox import bbox_overlaps
import math

DEBUG = False

class ProposalTargetLayer(caffe.Layer):
    """
    Assign object detection proposals to ground-truth targets. Produces proposal
    classification labels and bounding-box regression targets.
    """

    def setup(self, bottom, top):
        layer_params = yaml.load(self.param_str_)
        self._num_classes = layer_params['num_classes']

        # sampled rois (0, x1, y1, x2, y2)
        top[0].reshape(1, 5, 1, 1)
        # labels
        top[1].reshape(1, 1, 1, 1)
        # bbox_targets
        top[2].reshape(1, self._num_classes * 4, 1, 1)
        # bbox_inside_weights
        top[3].reshape(1, self._num_classes * 4, 1, 1)
        # bbox_outside_weights
        top[4].reshape(1, self._num_classes * 4, 1, 1)
        if cfg.VIEWPOINTS:
            self._viewp_bins = cfg.VIEWP_BINS
            if cfg.SMOOTH_L1_ANGLE:
                # viewpoints
                top[5].reshape(1, self._num_classes, 1, 1)
                # where viewpoints matter (which elements of top[5]?)
                top[6].reshape(1, self._num_classes, 1, 1)
            elif cfg.KL_ANGLE:
                # viewpoints
                top[5].reshape(1, self._viewp_bins, 1, 1)
                # where viewpoints matter (which elements of top[5]?)
                top[6].reshape(1, self._viewp_bins*self._num_classes, 1, 1)
            else:
                # viewpoints
                top[5].reshape(1, 1, 1, 1)
                # where viewpoints matter (which elements of top[5]?)
                top[6].reshape(1, self._viewp_bins*self._num_classes, 1, 1)
        else:
            self._viewp_bins = []

        if DEBUG:
            self._count = 0
            self._fg_num = 0
            self._bg_num = 0

    def forward(self, bottom, top):
        # Proposal ROIs (0, x1, y1, x2, y2) coming from RPN
        # (i.e., rpn.proposal_layer.ProposalLayer), or any other source
        all_rois = bottom[0].data
        # GT boxes (x1, y1, x2, y2, label, [viewpt])
        # TODO(rbg): it's annoying that sometimes I have extra info before
        # and other times after box coordinates -- normalize to one format
        gt_boxes = bottom[1].data
        gt_boxes = gt_boxes.reshape(gt_boxes.shape[0], gt_boxes.shape[1])

        if DEBUG and len(bottom)>2:
          img = bottom[2].data
        else:
          img=[]

        # Include ground-truth boxes in the set of candidate rois
        zeros = np.zeros((gt_boxes.shape[0], 1), dtype=gt_boxes.dtype)
        if cfg.VIEWPOINTS:
            all_rois = np.vstack(
                (all_rois, np.hstack((zeros, gt_boxes[:, :-2])))
            )
        else:
            all_rois = np.vstack(
                (all_rois, np.hstack((zeros, gt_boxes[:, :-1])))
            )

        # Sanity check: single batch only
        assert np.all(all_rois[:, 0] == 0), \
                'Only single item batches are supported'

        # num_images = 1
        rois_per_image = np.inf if cfg.TRAIN.BATCH_SIZE == -1 else cfg.TRAIN.BATCH_SIZE
        fg_rois_per_image = np.round(cfg.TRAIN.FG_FRACTION * rois_per_image)

        # Sample rois with classification labels and bounding box regression
        # targets
        labels, rois, bbox_targets, bbox_inside_weights, orientations, weights = _sample_rois(
            all_rois, gt_boxes, fg_rois_per_image,
            rois_per_image, self._num_classes, self._viewp_bins, img)

        if DEBUG:
            print 'num fg: {}'.format((labels > 0).sum())
            print 'num bg: {}'.format((labels == 0).sum())
            self._count += 1
            self._fg_num += (labels > 0).sum()
            self._bg_num += (labels == 0).sum()
            print 'num fg avg: {}'.format(self._fg_num / self._count)
            print 'num bg avg: {}'.format(self._bg_num / self._count)
            print 'ratio: {:.3f}'.format(float(self._fg_num) / float(self._bg_num))

        # sampled rois
        rois = rois.reshape((rois.shape[0], rois.shape[1], 1, 1))
        top[0].reshape(*rois.shape)
        top[0].data[...] = rois

        # classification labels
        labels = labels.reshape((labels.shape[0], 1, 1, 1))
        top[1].reshape(*labels.shape)
        top[1].data[...] = labels

        # bbox_targets
        bbox_targets = bbox_targets.reshape((bbox_targets.shape[0], bbox_targets.shape[1], 1, 1))
        top[2].reshape(*bbox_targets.shape)
        top[2].data[...] = bbox_targets

        # bbox_inside_weights
        bbox_inside_weights = bbox_inside_weights.reshape((bbox_inside_weights.shape[0], bbox_inside_weights.shape[1], 1, 1))
        top[3].reshape(*bbox_inside_weights.shape)
        top[3].data[...] = bbox_inside_weights

        # bbox_outside_weights
        bbox_inside_weights = bbox_inside_weights.reshape((bbox_inside_weights.shape[0], bbox_inside_weights.shape[1], 1, 1))
        top[4].reshape(*bbox_inside_weights.shape)
        top[4].data[...] = np.array(bbox_inside_weights > 0).astype(np.float32)

        if cfg.VIEWPOINTS:
            # orientations
            top[5].reshape(orientations.shape[0], orientations.shape[1], 1, 1)
            top[5].data[:,:,0,0] = orientations

            # where viewpoints matter (which elements of top[5]?)
            top[6].reshape(weights.shape[0], weights.shape[1], 1, 1)
            top[6].data[:,:,0,0] = weights

    def backward(self, top, propagate_down, bottom):
        """This layer does not propagate gradients."""
        pass

    def reshape(self, bottom, top):
        """Reshaping happens during the call to forward."""
        pass


def _get_bbox_regression_labels(bbox_target_data, num_classes):
    """Bounding-box regression targets (bbox_target_data) are stored in a
    compact form N x (class, tx, ty, tw, th)

    This function expands those targets into the 4-of-4*K representation used
    by the network (i.e. only one class has non-zero targets).

    Returns:
        bbox_target (ndarray): N x 4K blob of regression targets
        bbox_inside_weights (ndarray): N x 4K blob of loss weights
    """

    clss = bbox_target_data[:, 0]
    bbox_targets = np.zeros((clss.size, 4 * num_classes), dtype=np.float32)
    bbox_inside_weights = np.zeros(bbox_targets.shape, dtype=np.float32)
    inds = np.where(clss > 0)[0]
    for ind in inds:
        cls = clss[ind]
        start = int(4 * cls)
        end = start + 4
        bbox_targets[ind, start:end] = bbox_target_data[ind, 1:]
        bbox_inside_weights[ind, start:end] = cfg.TRAIN.BBOX_INSIDE_WEIGHTS
    return bbox_targets, bbox_inside_weights


def _compute_targets(ex_rois, gt_rois, labels):
    """Compute bounding-box regression targets for an image."""

    assert ex_rois.shape[0] == gt_rois.shape[0]
    assert ex_rois.shape[1] == 4
    assert gt_rois.shape[1] == 4

    targets = bbox_transform(ex_rois, gt_rois)
    if cfg.TRAIN.BBOX_NORMALIZE_TARGETS_PRECOMPUTED:
        # Optionally normalize targets by a precomputed mean and stdev
        targets = ((targets - np.array(cfg.TRAIN.BBOX_NORMALIZE_MEANS))
                / np.array(cfg.TRAIN.BBOX_NORMALIZE_STDS))
    return np.hstack(
            (labels[:, np.newaxis], targets)).astype(np.float32, copy=False)

def _sample_rois(all_rois, gt_boxes, fg_rois_per_image, rois_per_image, num_classes, num_bins=[], img=[]):
    """Generate a random sample of RoIs comprising foreground and background
    examples.
    """
    # overlaps: (rois x gt_boxes)
    overlaps = bbox_overlaps(
        np.ascontiguousarray(all_rois[:, 1:5], dtype=np.float),
        np.ascontiguousarray(gt_boxes[:, :4], dtype=np.float))

    # For each RoI: corresponding gt, overlap value, label and orientation
    gt_assignment = overlaps.argmax(axis=1)
    max_overlaps = overlaps.max(axis=1)
    labels = gt_boxes[gt_assignment, 4]
    if cfg.VIEWPOINTS:
        orientations = gt_boxes[gt_assignment, 5]

    dontcare_roi_inds = np.where(labels<0)[0]

    if DEBUG:
      print "~~~ PROPOSAL TARGET LAYER ~~~"

    # Provisional lists, see below
    care_max_overlaps_list = [max_overlaps[roi_ind]
                        if roi_ind not in dontcare_roi_inds else 0
                        for roi_ind, overlp in enumerate(max_overlaps)]
    dontcare_max_overlaps_list = [max_overlaps[roi_ind]
                        if roi_ind in dontcare_roi_inds else 0
                        for roi_ind, overlp in enumerate(max_overlaps)]

    # Max overlaps for each RoI, where...
    # Only RoIs assigned to "care" labels are != 0
    care_max_overlaps = np.array(care_max_overlaps_list)
    # Only RoIs assigned to "dontcare" labels are != 0
    dontcare_max_overlaps = np.array(dontcare_max_overlaps_list)

    # Select foreground RoIs as those with >= FG_THRESH overlap
    fg_inds = np.where(np.array(care_max_overlaps) >= cfg.TRAIN.FG_THRESH)[0]
    # Guard against the case when an image has fewer than fg_rois_per_image
    # foreground RoIs
    fg_rois_per_this_image = int(min(fg_rois_per_image, fg_inds.size))
    # Sample foreground regions without replacement
    if fg_inds.size > 0:
        fg_inds = npr.choice(fg_inds, size=fg_rois_per_this_image, replace=False)

    dontcare_gt_boxes_ind = np.where(gt_boxes[:,4]<0)
    dontcare_unaffected_anchors = np.all(overlaps[:,dontcare_gt_boxes_ind] <= cfg.TRAIN.RPN_DONTCARE_OVERLAP, axis=2)

    # Select background RoIs as those within [BG_THRESH_LO, BG_THRESH_HI)
    # BUT also make sure that "dontcare" RoIs are not used as background
    # even when BG_THRES_LO is set to 0
    bg_inds = np.where((care_max_overlaps < cfg.TRAIN.BG_THRESH_HI) &
                       (care_max_overlaps >= cfg.TRAIN.BG_THRESH_LO) &
                       (dontcare_max_overlaps <= cfg.TRAIN.DONTCARE_OVERLAP) &
                       dontcare_unaffected_anchors.ravel()) [0]

    # Compute number of background RoIs to take from this image (guarding
    # against there being fewer than desired)
    bg_rois_per_this_image = rois_per_image - fg_rois_per_this_image
    bg_rois_per_this_image = int(min(bg_rois_per_this_image, bg_inds.size))
    # Sample background regions without replacement
    if bg_inds.size > 0:
        bg_inds = npr.choice(bg_inds, size=bg_rois_per_this_image, replace=False)

    if DEBUG:
      print 'fg_inds'
      print fg_inds
      print 'bg_inds'
      print bg_inds

    # The indices that we're selecting (both fg and bg)
    keep_inds = np.append(fg_inds, bg_inds)
    # Select sampled values from various arrays:
    labels = labels[keep_inds]
    if cfg.VIEWPOINTS:

        orientations = orientations[keep_inds]
        orientations[fg_rois_per_this_image:] = -10
        if cfg.KL_ANGLE:
            angles = np.zeros((orientations.shape[0], cfg.VIEWP_BINS))
            weights = np.zeros((orientations.shape[0], num_bins*num_classes))
        elif cfg.SMOOTH_L1_ANGLE:
            angles = np.zeros((orientations.shape[0], num_classes))
            weights = np.zeros((orientations.shape[0], num_classes))
        else:
            angles = np.zeros((orientations.shape[0], 1))
            weights = np.zeros((orientations.shape[0], num_bins*num_classes))

        for ix, or_rads in enumerate(orientations):

            if cfg.CONTINUOUS_ANGLE:
                angles[ix] = or_rads
                weights[ix][0:num_bins] = 1
            elif cfg.SMOOTH_L1_ANGLE:
                if or_rads != -10 :
                    angles[ix][int(labels[ix])] = or_rads
                    weights[ix][int(labels[ix])] = 1
                else:
                    angles[ix][int(labels[ix])] = -10
            else:
                # Assign an orientation bin
                if or_rads != -10 :

                    weights[ix][int(labels[ix]*num_bins):int(labels[ix]*num_bins+num_bins)] = 1;
                    if or_rads < 0:
                        or_rads = or_rads + 2*math.pi
                    assert(cfg.VIEWP_OFFSET < (2*math.pi/cfg.VIEWP_BINS),
                           'Offsets greater than the bin itself are not implemented')
                    nbin = math.floor((or_rads+cfg.VIEWP_OFFSET)/(2*math.pi/cfg.VIEWP_BINS))
                    if nbin>cfg.VIEWP_BINS-1:
                        nbin = 0

                    if not cfg.KL_ANGLE:
                        angles[ix]=nbin
                    else:
                        # Kullback-Leibler divergence. +info:
                        #   L. Yang, J. Liu, and X. Tang,
                        #   "Object detection and viewpoint estimation with auto-masking neural network,"
                        #   in Computer Vision - ECCV 2014, 2014, pp. 441-455.
                        bin_center = (math.pi*(2.*nbin+1))/cfg.VIEWP_BINS - cfg.VIEWP_OFFSET
                        L = 2. * math.pi / cfg.VIEWP_BINS
                        if or_rads<bin_center:
                            bin_minus = nbin - 1
                            bin_plus = nbin
                            if bin_minus < 0:
                                bin_minus = cfg.VIEWP_BINS-1
                                bin_center_minus = (math.pi*(2.*(cfg.VIEWP_BINS-1)+1))/cfg.VIEWP_BINS - cfg.VIEWP_OFFSET
                            else:
                                bin_center_minus = (math.pi*(2.*(nbin-1)+1))/cfg.VIEWP_BINS - cfg.VIEWP_OFFSET
                            bin_center_plus = bin_center
                        elif or_rads>bin_center:
                            bin_minus = nbin
                            bin_plus = nbin + 1
                            bin_center_minus = bin_center
                            if bin_plus>cfg.VIEWP_BINS-1:
                                bin_plus = 0
                                bin_center_plus = (math.pi*(2.*(0)+1))/cfg.VIEWP_BINS - cfg.VIEWP_OFFSET
                            else:
                                bin_center_plus = (math.pi*(2.*(nbin+1)+1))/cfg.VIEWP_BINS - cfg.VIEWP_OFFSET

                        if or_rads==bin_center:
                            angles[ix][nbin] = 1
                        else:
                            angles[ix][int(bin_minus)] = (bin_center_plus - or_rads) / L
                            angles[ix][int(bin_plus)] = (or_rads - bin_center_minus) / L
                else:
                    if not cfg.KL_ANGLE:
                        weights[ix][0:num_bins] = 1
                        angles[ix] = -10
                    else:
                        weights[ix][0:num_bins] = 1
                        angles[ix][...] = -10

    else:
        angles = []
        weights = []

    # Clamp labels for the background RoIs to 0
    labels[fg_rois_per_this_image:] = 0
    rois = all_rois[keep_inds]

    bbox_target_data = _compute_targets(
        rois[:, 1:5], gt_boxes[gt_assignment[keep_inds], :4], labels)

    bbox_targets, bbox_inside_weights = \
        _get_bbox_regression_labels(bbox_target_data, num_classes)

    if DEBUG:
      _vis_whats_happening(img, rois, labels, keep_inds)

    return labels, rois, bbox_targets, bbox_inside_weights, angles, weights

def _vis_whats_happening(im_blob, rois, labels, keep_inds):
    """Visualize a mini-batch for debugging."""
    CLASS_COLOR = ((1,1,1), (0,0,0),
        (0,0,1), (0,0.5,1),(0,0.75,1),(1,0,0),
        (1,0.5,0.5),(0,1,1), (1,1,1), (0,0,0))

    import matplotlib.pyplot as plt
    assert len(im_blob)>0, 'Please modify the prototxt'
    im = im_blob[0, :, :, :].transpose((1, 2, 0)).copy()
    im += cfg.PIXEL_MEANS
    im = im[:, :, (2, 1, 0)]
    im = im.astype(np.uint8)
    plt.figure("ROIS to Fast-RCNN")
    plt.imshow(im)

    for i in xrange(rois.shape[0]):
        color = np.random.rand(3,1)
        lwidth = 4 if labels[i]>0 else 1
        roi = rois[i, 1:5]
        plt.gca().add_patch(
            plt.Rectangle((roi[0], roi[1]), roi[2] - roi[0],
                roi[3] - roi[1], fill=False,
                edgecolor=color, linewidth=lwidth)
            )

        #   plt.gca().annotate(keep_inds[i], (roi[0], roi[3]), color='w', weight='bold',
        #         fontsize=8, ha='center', va='center')

    plt.show()
