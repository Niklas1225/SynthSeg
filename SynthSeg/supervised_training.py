# python imports
import os
import numpy as np
import tensorflow as tf
import keras.layers as KL
import keras.backend as K
import numpy.random as npr
from keras.models import Model

# project imports
from SynthSeg import metrics_model
from SynthSeg.training import train_model

# third-party imports
from ext.lab2im import utils
from ext.lab2im import edit_volumes
from ext.neuron import models as nrn_models
from ext.neuron import layers as nrn_layers
from ext.lab2im import edit_tensors as l2i_et
from ext.lab2im import spatial_augmentation as l2i_sa
from ext.lab2im import intensity_augmentation as l2i_ia


def supervised_training(image_dir,
                        labels_dir,
                        model_dir,
                        path_segmentation_labels=None,
                        batchsize=1,
                        output_shape=None,
                        flipping=True,
                        scaling_bounds=.015,
                        rotation_bounds=15,
                        shearing_bounds=.012,
                        translation_bounds=False,
                        nonlin_std=3.,
                        nonlin_shape_factor=.04,
                        bias_field_std=.3,
                        bias_shape_factor=.025,
                        n_levels=5,
                        nb_conv_per_level=2,
                        conv_size=3,
                        unet_feat_count=24,
                        feat_multiplier=2,
                        dropout=0,
                        activation='elu',
                        lr=1e-4,
                        lr_decay=0,
                        wl2_epochs=5,
                        dice_epochs=100,
                        steps_per_epoch=1000,
                        load_model_file=None,
                        initial_epoch_wl2=0,
                        initial_epoch_dice=0):

    # check epochs
    assert (wl2_epochs > 0) | (dice_epochs > 0), \
        'either wl2_epochs or dice_epochs must be positive, had {0} and {1}'.format(wl2_epochs, dice_epochs)

    # prepare data files
    path_images = utils.list_images_in_folder(image_dir)
    path_labels = utils.list_images_in_folder(labels_dir)
    assert len(path_images) == len(path_labels), "There should be as many images as label maps."

    # get label lists
    label_list, n_neutral_labels = utils.get_list_labels(label_list=path_segmentation_labels, labels_dir=labels_dir,
                                                         FS_sort=True)
    n_labels = np.size(label_list)

    # prepare model folder
    utils.mkdir(model_dir)

    # prepare log folder
    log_dir = os.path.join(model_dir, 'logs')
    utils.mkdir(log_dir)

    # create augmentation model and input generator
    im_shape, _, _, n_channels, _, _ = utils.get_volume_info(path_images[0], aff_ref=np.eye(4))
    augmentation_model = build_augmentation_model(im_shape,
                                                  n_channels,
                                                  label_list,
                                                  n_neutral_labels,
                                                  output_shape=output_shape,
                                                  output_div_by_n=2 ** n_levels,
                                                  flipping=flipping,
                                                  aff=np.eye(4),
                                                  scaling_bounds=scaling_bounds,
                                                  rotation_bounds=rotation_bounds,
                                                  shearing_bounds=shearing_bounds,
                                                  translation_bounds=translation_bounds,
                                                  nonlin_std=nonlin_std,
                                                  nonlin_shape_factor=nonlin_shape_factor,
                                                  bias_field_std=bias_field_std,
                                                  bias_shape_factor=bias_shape_factor)
    unet_input_shape = augmentation_model.output[0].get_shape().as_list()[1:]

    model_input_generator = build_model_inputs(path_images,
                                               path_labels,
                                               batchsize=batchsize)
    training_generator = utils.build_training_generator(model_input_generator, batchsize)

    # prepare the segmentation model
    unet_model = nrn_models.unet(nb_features=unet_feat_count,
                                 input_shape=unet_input_shape,
                                 nb_levels=n_levels,
                                 conv_size=conv_size,
                                 nb_labels=n_labels,
                                 feat_mult=feat_multiplier,
                                 nb_conv_per_level=nb_conv_per_level,
                                 conv_dropout=dropout,
                                 batch_norm=-1,
                                 activation=activation,
                                 input_model=augmentation_model)

    # pre-training with weighted L2, input is fit to the softmax rather than the probabilities
    if wl2_epochs > 0:
        wl2_model = Model(unet_model.inputs, [unet_model.get_layer('unet_likelihood').output])
        wl2_model = metrics_model.metrics_model(input_shape=unet_input_shape[:-1] + [n_labels],
                                                segmentation_label_list=label_list,
                                                input_model=wl2_model,
                                                metrics='wl2',
                                                name='metrics_model')
        if load_model_file is not None:
            print('loading ', load_model_file)
            wl2_model.load_weights(load_model_file)
        train_model(wl2_model, training_generator, lr, lr_decay, wl2_epochs, steps_per_epoch, model_dir, log_dir,
                    'wl2', initial_epoch_wl2)

    # fine-tuning with dice metric
    if dice_epochs > 0:
        dice_model = metrics_model.metrics_model(input_shape=unet_input_shape[:-1] + [n_labels],
                                                 segmentation_label_list=label_list,
                                                 input_model=unet_model,
                                                 name='metrics_model')
        if wl2_epochs > 0:
            last_wl2_model_name = os.path.join(model_dir, 'wl2_%03d.h5' % wl2_epochs)
            dice_model.load_weights(last_wl2_model_name, by_name=True)
        elif load_model_file is not None:
            print('loading ', load_model_file)
            dice_model.load_weights(load_model_file)
        train_model(dice_model, training_generator, lr, lr_decay, dice_epochs, steps_per_epoch, model_dir, log_dir,
                    'dice', initial_epoch_dice)


def build_augmentation_model(im_shape,
                             n_channels,
                             segmentation_labels,
                             n_neutral_labels,
                             output_shape=None,
                             output_div_by_n=None,
                             flipping=True,
                             aff=None,
                             scaling_bounds=0.15,
                             rotation_bounds=15,
                             shearing_bounds=0.012,
                             translation_bounds=False,
                             nonlin_std=3.,
                             nonlin_shape_factor=.0625,
                             bias_field_std=.3,
                             bias_shape_factor=.025):

    # reformat resolutions and get shapes
    im_shape = utils.reformat_to_list(im_shape)
    n_dims, _ = utils.get_dims(im_shape)
    crop_shape = get_shapes(im_shape, output_shape, output_div_by_n)

    # create new_label_list and corresponding LUT to make sure that labels go from 0 to N-1
    new_seg_labels, lut = utils.rearrange_label_list(segmentation_labels)

    # define model inputs
    image_input = KL.Input(shape=im_shape+[n_channels], name='image_input')
    labels_input = KL.Input(shape=im_shape + [1], name='labels_input')

    # convert labels to new_label_list
    labels = l2i_et.convert_labels(labels_input, lut)

    # deform labels
    if (scaling_bounds is not False) | (rotation_bounds is not False) | (shearing_bounds is not False) | \
       (translation_bounds is not False) | (nonlin_std is not False):
        labels._keras_shape = tuple(labels.get_shape().as_list())
        labels, image = layers.RandomSpatialDeformation(scaling_bounds=scaling_bounds,
                                                        rotation_bounds=rotation_bounds,
                                                        shearing_bounds=shearing_bounds,
                                                        translation_bounds=translation_bounds,
                                                        nonlin_std=nonlin_std,
                                                        nonlin_shape_factor=nonlin_shape_factor,
                                                        inter_method=['nearest', 'linear'])([labels, image_input])
    else:
        image = image_input

    # crop labels
    if crop_shape != im_shape:
        labels._keras_shape = tuple(labels.get_shape().as_list())
        image._keras_shape = tuple(image.get_shape().as_list())
        labels, image = layers.RandomCrop(crop_shape)([labels, image])

    # flip labels
    if flipping:
        assert aff is not None, 'aff should not be None if flipping is True'
        labels._keras_shape = tuple(labels.get_shape().as_list())
        image._keras_shape = tuple(image.get_shape().as_list())
        labels, image = layers.RandomFlip(flip_axis=get_ras_axes(aff, n_dims)[0], swap_labels=[True, False],
                                          label_list=new_seg_labels, n_neutral_labels=n_neutral_labels)([labels, image])

    # apply bias field
    if bias_field_std > 0:
        image._keras_shape = tuple(image.get_shape().as_list())
        image = layers.BiasFieldCorruption(bias_field_std, bias_shape_factor, False)(image)
        image = KL.Lambda(lambda x: tf.cast(x, dtype='float32'), name='image_biased')(image)

    # intensity augmentation
    image._keras_shape = tuple(image.get_shape().as_list())
    image = layers.IntensityAugmentation(0, clip=False, normalise=True, gamma_std=.5, separate_channels=True)(image)
    image = KL.Lambda(lambda x: tf.cast(x, dtype='float32'), name='image_augmented')(image)

    # convert labels back to original values and reset unwanted labels to zero
    labels = l2i_et.convert_labels(labels, segmentation_labels)
    labels = KL.Lambda(lambda x: tf.cast(x, dtype='int32'), name='labels_out')(labels)

    # build model (dummy layer enables to keep the labels when plugging this model to other models)
    image = KL.Lambda(lambda x: x[0], name='image_out')([image, labels])
    brain_model = Model(inputs=list_inputs, outputs=[image, labels])

    return brain_model


def build_model_inputs(path_images,
                       path_label_maps,
                       batchsize=1):

    # get label info
    _, _, n_dims, n_channels, _, _ = utils.get_volume_info(path_images[0])

    # Generate!
    while True:

        # randomly pick as many images as batchsize
        indices = npr.randint(len(path_label_maps), size=batchsize)

        # initialise input lists
        list_images = list()
        list_label_maps = list()

        for idx in indices:

            # add image
            image = utils.load_volume(path_images[idx], aff_ref=np.eye(4))
            if n_channels > 1:
                list_images.append(utils.add_axis(image, axis=0))
            else:
                list_images.append(utils.add_axis(image, axis=[0, -1]))

            # add labels
            labels = utils.load_volume(path_label_maps[idx], dtype='int', aff_ref=np.eye(4))
            list_label_maps.append(utils.add_axis(labels, axis=[0, -1]))

        # build list of inputs of augmentation model
        list_inputs = [list_images, list_label_maps]
        if batchsize > 1:  # concatenate individual input types if batchsize > 1
            list_inputs = [np.concatenate(item, 0) for item in list_inputs]
        else:
            list_inputs = [item[0] for item in list_inputs]

        yield list_inputs


def get_shapes(im_shape, crop_shape, output_div_by_n):

    # reformat resolutions to lists
    im_shape = utils.reformat_to_list(im_shape)
    n_dims = len(im_shape)

    # crop_shape specified
    if crop_shape is not None:
        crop_shape = utils.reformat_to_list(crop_shape, length=n_dims, dtype='int')

        # make sure that crop_shape is smaller or equal to label shape
        crop_shape = [min(im_shape[i], crop_shape[i]) for i in range(n_dims)]

        # make sure crop_shape is divisible by output_div_by_n
        if output_div_by_n is not None:
            tmp_shape = [utils.find_closest_number_divisible_by_m(s, output_div_by_n, smaller_ans=True)
                         for s in crop_shape]
            if crop_shape != tmp_shape:
                print('crop_shape {0} not divisible by {1}, changed to {2}'.format(crop_shape, output_div_by_n,
                                                                                   tmp_shape))
                crop_shape = tmp_shape

    # no crop_shape specified, so no cropping unless label_shape is not divisible by output_div_by_n
    else:

        # make sure output shape is divisible by output_div_by_n
        if output_div_by_n is not None:
            crop_shape = [utils.find_closest_number_divisible_by_m(s, output_div_by_n, smaller_ans=True)
                          for s in im_shape]

        # if no need to be divisible by n, simply take labels_shape
        else:
            crop_shape = im_shape

    return crop_shape
