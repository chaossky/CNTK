# ==============================================================================
# Copyright (c) Microsoft. All rights reserved.
# Licensed under the MIT license. See LICENSE.md file in the project root
# for full license information.
# ==============================================================================

import sys
import numpy

from operator import mul
from functools import reduce

import cntk
import cntk.io.transforms as xforms
from cntk import ops, io, learner, internal
from cntk import Trainer
from cntk.layers.blocks import BlockFunction


class BlockApiSetup(object):
    @staticmethod
    def convolution(output, kernel, stride, pad, kernel_init, bias_init, group, dilation, name):
        def conv_ops(weights, data):
            return ops.convolution(weights, data, strides=(cntk.InferredDimension, ) + ops.sanitize_shape(stride),
                                   auto_padding=[pad])

        def weights_parameter(output_channels, init, group_name):
            dilation_kernel = [(k - 1) * d + 1 for k, d in zip(kernel, dilation)]
            # expand kernel to simulate dilation
            used_init = init.copy()
            if dilation_kernel != kernel:
                for axis in range(len(dilation)):
                    kernel_sequence = [x * dilation[axis] for x in range(kernel[axis])]
                    insert_lines = list(set([x for x in range(dilation_kernel[axis])]) ^ set(kernel_sequence))
                    for index in range(len(insert_lines)):
                        insert_lines[index] -= index
                    used_init = numpy.insert(used_init, insert_lines, 0, axis=len(init.shape) - axis - 1)
            return ops.parameter(shape=(output_channels, cntk.InferredDimension) + ops.sanitize_shape(dilation_kernel),
                                 init=used_init, name=group_name)

        if group == 1:
            w = weights_parameter(output, kernel_init, '.'.join((name, 'W')))
        else:
            sub_output_channels = int(output / group)
            groups_kernel_init = numpy.split(kernel_init, group)
            groups_kernel = [weights_parameter(sub_output_channels, groups_kernel_init[i], '.'.join((name, str(i), 'W')))
                             for i in range(0, group)]
            sub_input_channels = groups_kernel[0].shape[1]
        if bias_init is not None:
            b = ops.parameter(shape=(output, ), init=bias_init, name='.'.join((name, 'b')))

        @BlockFunction('Convolution', name)
        def convolution(x):
            if group == 1:
                apply_x = conv_ops(w, x)
            else:
                groups_data = [ops.slice(x, axis=0, begin_index=i * sub_input_channels,
                                         end_index=(i + 1) * sub_input_channels) for i in range(0, group)]
                apply_sub = [conv_ops(group_kernel, group_data)
                             for group_kernel, group_data in zip(groups_kernel, groups_data)]
                apply_x = ops.splice(*apply_sub, axis=0)
            if bias_init is not None:
                apply_x += b
            return apply_x
        return convolution

    @staticmethod
    def linear(output_shape, input_shape, scale_init, bias_init, name):
        sc = ops.parameter(shape=input_shape + output_shape, init=scale_init, name='.'.join((name, 'sc')))
        b = ops.parameter(shape=output_shape, init=bias_init, name='.'.join((name, 'b')))

        @BlockFunction('linear', name)
        def linear(x):
            apply_x = ops.times(x, sc)
            apply_x += b
            return apply_x
        return linear

    @staticmethod
    def lrn(k, n, alpha, beta, name):
        @BlockFunction('lrn', name)
        def lrn(x):
            x2 = cntk.ops.square(x)
            x2s = cntk.ops.reshape(x2, (1, cntk.InferredDimension), 0, 1)
            w = cntk.ops.constant(alpha / (2 * n - 1), (1, 2 * n - 1, 1, 1), name='W')
            y = cntk.ops.convolution(w, x2s)
            # reshape back to remove the fake singleton reduction dimension
            b = cntk.ops.reshape(y, cntk.InferredDimension, 0, 2)
            den = cntk.ops.exp(beta * cntk.ops.log(k + b))
            apply_x = cntk.ops.element_divide(x, den)
            return apply_x
        return lrn


class ApiSetup(object):
    # TODO: Dangerous call, verify next
    @staticmethod
    def convolution(cntk_layer, inputs):
        sanitize_input = internal.sanitize_input(inputs[0])
        params = cntk_layer.parameters
        output_channel = params.output
        kernel_size = params.kernel
        # TODO: Add init type
        kernel_init = None
        if cntk_layer.parameter_tensor:
            kernel_data_tensor = cntk_layer.parameter_tensor[0]
            kernel_init = numpy.asarray(kernel_data_tensor.data, dtype=numpy.float32)
            # TODO: here
        bias_shape = (output_channel, ) + (1,) * 2
        bias_init = None
        if params.need_bias:
            if cntk_layer.parameter_tensor:
                bias_data_tensor = cntk_layer.parameter_tensor[1]
                # TODO: How to throw warning
                bias_init = numpy.asarray(bias_data_tensor.data, dtype=numpy.float32)
                bias_init = numpy.reshape(bias_init, bias_shape)
        return BlockApiSetup.convolution(output_channel, kernel_size, stride=params.stride, pad=params.auto_pad,
                                         kernel_init=kernel_init, bias_init=bias_init,
                                         group=params.group, dilation=params.dilation,
                                         name=cntk_layer.op_name)(sanitize_input)

    @staticmethod
    def batch_normalization(cntk_layer, inputs):
        sanitize_input = internal.sanitize_input(inputs[0])
        parameter_tensor = (sanitize_input.shape[0], )
        scale_init = 1
        bias_init = 0
        mean_init = 1
        var_init = 0
        if cntk_layer.parameter_tensor:
            if len(cntk_layer.parameter_tensor) < 3:
                raise AssertionError('At least three tensors (saved_mean, saved_variance and scale) are needed')
            mean_tensor = cntk_layer.parameter_tensor[0]
            variance_tensor = cntk_layer.parameter_tensor[1]
            global_scale = cntk_layer.parameter_tensor[2].data[0]
            scale_init = 1 / global_scale if global_scale != 0 else 0
            mean_init = numpy.asarray(mean_tensor.data, dtype=numpy.float32) * scale_init
            var_init = numpy.asarray(variance_tensor.data, dtype=numpy.float32) * scale_init
            if len(cntk_layer.parameter_tensor) == 5:
                scale_tensor = cntk_layer.parameter_tensor[3]
                bias_tensor = cntk_layer.parameter_tensor[4]
                scale_init = numpy.asarray(scale_tensor.data, dtype=numpy.float32)
                bias_init = numpy.asarray(bias_tensor.data, dtype=numpy.float32)

        scale_parameters = ops.parameter(parameter_tensor, init=scale_init, name='.'.join((cntk_layer.op_name, 'scale')))
        bias_parameters = ops.parameter(parameter_tensor, init=bias_init, name='.'.join((cntk_layer.op_name, 'bias')))
        mean_parameters = ops.parameter(parameter_tensor, init=mean_init, name='.'.join((cntk_layer.op_name, 'mean')))
        var_parameters = ops.parameter(parameter_tensor, init=var_init, name='.'.join((cntk_layer.op_name, 'var')))
        epsilon = cntk_layer.parameters.epsilon

        return ops.batch_normalization(sanitize_input, scale_parameters, bias_parameters, mean_parameters,
                                       var_parameters, True, use_cudnn_engine=False, epsilon=epsilon,
                                       running_count=ops.constant(0),
                                       name=cntk_layer.op_name)

    @staticmethod
    def pooling(cntk_layer, inputs):
        sanitize_input = internal.sanitize_input(inputs[0])
        pooling_type = ops.PoolingType_Average if cntk_layer.parameters.pooling_type else ops.PoolingType_Max
        return ops.pooling(sanitize_input, pooling_type, tuple(cntk_layer.parameters.kernel),
                           strides=tuple(cntk_layer.parameters.stride),
                           auto_padding=[cntk_layer.parameters.auto_pad],
                           ceil_out_dim=True,
                           name=cntk_layer.op_name)

    @staticmethod
    def relu(cntk_layer, inputs):
        sanitize_input = internal.sanitize_input(inputs[0])
        return ops.relu(sanitize_input, name=cntk_layer.op_name)

    @staticmethod
    def dense(cntk_layer, inputs):
        sanitize_input = internal.sanitize_input(inputs[0])
        input_channel = sanitize_input.shape
        output_channel = cntk_layer.parameters.num_output

        flattened_channel = reduce(mul, list(input_channel))
        scale_shape = input_channel + (output_channel, )
        bias_shape = (output_channel, )

        if cntk_layer.parameter_tensor:
            if len(cntk_layer.parameter_tensor) != 2:
                raise AssertionError('dense layer layer receives two inputs (scale/bias)')
            scale_tensor = cntk_layer.parameter_tensor[0]
            bias_tensor = cntk_layer.parameter_tensor[1]
            scale_init = numpy.asarray(scale_tensor.data, numpy.float32)
            if cntk_layer.parameters.transpose:
                scale_init = numpy.reshape(scale_init, (output_channel, flattened_channel))
                scale_init = numpy.transpose(scale_init).copy()
                scale_init = numpy.reshape(scale_init, scale_shape)
            else:
                scale_init = numpy.reshape(scale_init, scale_shape)
            bias_init = numpy.asarray(bias_tensor.data, numpy.float32)
        return BlockApiSetup.linear(bias_shape, scale_shape, scale_init, bias_init, cntk_layer.op_name)(sanitize_input)

    @staticmethod
    def plus(cntk_layer, inputs):
        sanitize_left = ops.sanitize_input(inputs[0])
        sanitize_right = ops.sanitize_input(inputs[1])
        return ops.plus(sanitize_left, sanitize_right, name=cntk_layer.op_name)

    @staticmethod
    def classification_error(cntk_layer, inputs):
        sanitize_output = ops.sanitize_input(inputs[0])
        sanitize_label = ops.sanitize_input(inputs[1])
        return ops.classification_error(sanitize_output, sanitize_label, topN=cntk_layer.parameters.top_n,
                                        name=cntk_layer.op_name)

    @staticmethod
    def cross_entropy_with_softmax(cntk_layer, inputs):
        sanitize_output = ops.sanitize_input(inputs[0])
        sanitize_label = ops.sanitize_input(inputs[1])
        return ops.cross_entropy_with_softmax(sanitize_output, sanitize_label, name=cntk_layer.op_name)

    @staticmethod
    def dropout(cntk_layer, inputs):
        sanitize_output = ops.sanitize_input(inputs[0])
        return ops.dropout(sanitize_output, name=cntk_layer.op_name)

    @staticmethod
    def lrn(cntk_layer, inputs):
        sanitize_output = ops.sanitize_input(inputs[0])
        params = cntk_layer.parameters
        return BlockApiSetup.lrn(params.k, params.kernel_size, params.alpha,
                                 params.beta, cntk_layer.op_name)(sanitize_output)

    @staticmethod
    def splice(cntk_layer, inputs):
        return ops.splice(*inputs, axis=0, name=cntk_layer.op_name)

    @staticmethod
    def psroi_pooling(cntk_layer, inputs):
        conv_map = ops.sanitize_input(inputs[0])
        rois = ops.sanitize_input(inputs[1])
        params = cntk_layer.parameters
        return ops.psroipooling(conv_map, rois, params.group_size, params.out_channel, name=cntk_layer.op_name)

    @staticmethod
    def softmax(cntk_layer, inputs):
        santize_output = ops.sanitize_input(inputs[0])
        return ops.softmax(santize_output, name=cntk_layer.op_name)


class CntkApiInstance(object):
    def __init__(self, cntk_uni_model, global_conf):
        self._functions = {}
        self._output = None
        self._model_solver = global_conf.model_solver
        self._source_solver = global_conf.source_solver
        self.__instance__(cntk_uni_model)

    def __instance__(self, cntk_uni_model):
        self.instance_input(cntk_uni_model.data_provider)
        self.instance_functions(cntk_uni_model.cntk_sorted_layers, cntk_uni_model.cntk_layers)

    def instance_input(self, data_providers):
        if self._model_solver.cntk_tensor is not None:
            for key, tensor in self._model_solver.cntk_tensor.items():
                input_var = cntk.input(tuple(tensor), name=key)
                self._functions[key] = input_var
        else:
            for data_provider in data_providers:
                input_var = cntk.input(tuple(data_provider.tensor[:]), name=data_provider.op_name)
                self._functions[data_provider.op_name] = input_var

    def instance_functions(self, cntk_sorted_layers, cntk_layers):
        usused_func = set()
        for cntk_sorted_layer in cntk_sorted_layers:
            cntk_layer = cntk_layers[cntk_sorted_layer]
            local_inputs = []
            for local_input in cntk_layer.inputs:
                local_inputs.append(self._functions[local_input])
                if self._functions[local_input] in usused_func:
                    usused_func.remove(self._functions[local_input])
            self._functions[cntk_layer.op_name] = getattr(ApiSetup, cntk_layer.op_type.name)(cntk_layer, local_inputs)
            usused_func.add(self._functions[cntk_layer.op_name])
        self._output = ops.combine(list(usused_func), name='outputs')

    def export_model(self):
        save_path = self._model_solver.cntk_model_path
        self._output.save_model(save_path)

    def get_model(self):
        return self._output

    def get_functions(self):
        return self._functions


class Evaluator(object):
    def __init__(self, global_conf, models):
        self._eval_solver = global_conf.classify_eval_solver
        self._model = models

    def create_reader(self, map_file, mean_file, input_tensor):
        transforms = []
        if self._eval_solver.crop_type:
            transforms += [xforms.crop(crop_type=self._eval_solver.crop_type,
                                       side_ratio=self._eval_solver.crop_ratio)]
        transforms += [
            xforms.scale(width=input_tensor[1], height=input_tensor[2], channels=input_tensor[0],
                         interpolations='linear'),
            xforms.mean(mean_file)
        ]

        return io.MinibatchSource(io.ImageDeserializer(map_file, io.StreamDefs(
            features=io.StreamDef(field='image', transforms=transforms),
            labels=io.StreamDef(field='label', shape=input_tensor[3]))))

    def eval_model(self):
        sys.stdout.flush()
        sys.stdout.write('start eval...\n')
        sys.stdout.write('launch map and mean files\n')
        map_file = self._eval_solver.index_map
        mean_file = self._eval_solver.mean_file
        if map_file is None:
            sys.stdout.write('fail to locate index files, eval exit.\n')
            return
        # Fixed tensor
        data = self._model.arguments[0]
        label = cntk.input(tuple(self._eval_solver.label_tensor), name='label')
        reader_tensor = data.shape + label.shape
        reader_test = self.create_reader(map_file, mean_file, reader_tensor)
        output = self._model.outputs[0]
        output_tensor = ops.sanitize_input(output).shape
        flattened_channel = reduce(mul, list(output_tensor))
        output = ops.reshape(output, (flattened_channel, ))
        ce = cntk.cross_entropy_with_softmax(output, label)
        pe = cntk.classification_error(output, label, topN=self._eval_solver.top_n)
        trainer = Trainer(output, (ce, pe), learner.sgd(output.parameters, lr=learner.learning_rate_schedule(
            0, learner.UnitType.minibatch)))
        input_map = {
            data: reader_test.streams.features,
            label: reader_test.streams.labels
        }
        num_samples = self._eval_solver.dataset_size
        test_minibatch_size = self._eval_solver.batch_size
        test_result = 0.0
        for i in range(0, int(num_samples / test_minibatch_size)):
            mb = reader_test.next_minibatch(test_minibatch_size, input_map=input_map)
            eval_error = trainer.test_minibatch(mb)
            test_result += eval_error
            if i % 100 == 0:
                sys.stdout.write('Evaluate error with %s with test range %s...\n'
                                 % (str(test_result / (i + 1)),
                                    str(i * test_minibatch_size)))
                sys.stdout.flush()
        sys.stdout.write('Final evaluate error with %s\n' % str(test_result / int(num_samples / test_minibatch_size)))